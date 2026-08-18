"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { GLOBE_RADIUS, anchorOf, toVector3 } from "@/lib/geo";
import type { GeoLines } from "@/lib/geo";
import { colorOf } from "@/lib/severity";
import type { Feature, SearchHit } from "@/lib/types";

// public/ files are not rewritten by assetPrefix, so the prefix has to be
// applied by hand: in production Flask mounts the export under /static.
const ASSET_PREFIX = process.env.NEXT_PUBLIC_ASSET_PREFIX ?? "";

interface Props {
  features: Feature[];
  hits: SearchHit[];
  selectedId: string | null;
  hoveredId: string | null;
  onSelect: (id: string | null) => void;
  onHover: (id: string | null) => void;
}

interface Placed {
  feature: Feature;
  position: THREE.Vector3;
}

/** Similarity to column height. Anchored to an absolute scale, not to the range
 *  of the visible hits: a set of weak matches should look short rather than be
 *  stretched to fill the axis. */
const columnHeight = (similarity: number) =>
  0.03 + 0.15 * Math.min(1, Math.max(0, (similarity - 0.25) / 0.55));

export default function Globe({
  features,
  hits,
  selectedId,
  hoveredId,
  onSelect,
  onHover,
}: Props) {
  const mountRef = useRef<HTMLDivElement>(null);
  const [geo, setGeo] = useState<GeoLines | null>(null);
  const [tip, setTip] = useState<{ x: number; y: number; feature: Feature } | null>(null);

  // Three.js objects live outside React state: they are mutated every frame and
  // re-rendering the tree for them would be both wasteful and wrong.
  const scene = useRef<THREE.Scene>(null!);
  const camera = useRef<THREE.PerspectiveCamera>(null!);
  const renderer = useRef<THREE.WebGLRenderer>(null!);
  const controls = useRef<OrbitControls>(null!);
  const dataGroup = useRef<THREE.Group>(null!);
  const markerCloud = useRef<THREE.Points | null>(null);
  const placed = useRef<Placed[]>([]);
  const flight = useRef<{ from: THREE.Vector3; to: THREE.Vector3; t: number } | null>(null);

  // Latest callbacks, read from the animation loop without re-binding listeners.
  const handlers = useRef({ onSelect, onHover });
  handlers.current = { onSelect, onHover };

  useEffect(() => {
    fetch(`${ASSET_PREFIX}/geo.json`)
      .then((r) => r.json())
      .then(setGeo)
      .catch(() => setGeo({ land: [], states: [], nation: [] }));
  }, []);

  /* --- scene, once ------------------------------------------------------- */
  useEffect(() => {
    const mount = mountRef.current;
    if (!mount) return;

    const s = new THREE.Scene();
    scene.current = s;

    const cam = new THREE.PerspectiveCamera(38, mount.clientWidth / mount.clientHeight, 0.01, 100);
    // Framed on the continental US: every document in this corpus is a US
    // National Weather Service product, so opening anywhere else would just
    // make the first interaction be "find the data".
    cam.position.copy(toVector3(-97, 27, 2.55));
    camera.current = cam;

    const r = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    r.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    r.setSize(mount.clientWidth, mount.clientHeight);
    mount.appendChild(r.domElement);
    renderer.current = r;

    const c = new OrbitControls(cam, r.domElement);
    c.enableDamping = true;
    c.dampingFactor = 0.075;
    c.rotateSpeed = 0.42;
    c.enablePan = false;
    c.minDistance = 1.25;
    c.maxDistance = 5.5;
    c.zoomSpeed = 0.7;
    controls.current = c;

    // Ocean. Slightly smaller than the line layers so coastlines never z-fight
    // with the surface they sit on.
    const ocean = new THREE.Mesh(
      new THREE.SphereGeometry(GLOBE_RADIUS * 0.998, 64, 48),
      new THREE.MeshBasicMaterial({ color: 0x0c1c2f })
    );
    s.add(ocean);

    // Atmosphere: a back-face sphere whose opacity rises at grazing angles, so
    // the limb glows and the centre stays clear. Cheaper and steadier than a
    // post-processed bloom, which would also blow out the marker colours.
    const atmosphere = new THREE.Mesh(
      new THREE.SphereGeometry(GLOBE_RADIUS * 1.16, 48, 36),
      new THREE.ShaderMaterial({
        transparent: true,
        side: THREE.BackSide,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
        uniforms: { uColor: { value: new THREE.Color(0x2f7fae) } },
        vertexShader: `
          varying vec3 vNormal;
          void main() {
            vNormal = normalize(normalMatrix * normal);
            gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
          }`,
        fragmentShader: `
          uniform vec3 uColor;
          varying vec3 vNormal;
          void main() {
            float rim = pow(0.72 - dot(vNormal, vec3(0.0, 0.0, 1.0)), 3.0);
            gl_FragColor = vec4(uColor, 1.0) * clamp(rim, 0.0, 1.0) * 0.85;
          }`,
      })
    );
    s.add(atmosphere);

    // Graticule every 15 degrees - a faint reminder that this is a sphere, and
    // a scale reference when zoomed in.
    const grat: number[] = [];
    for (let lat = -75; lat <= 75; lat += 15) {
      for (let lon = -180; lon < 180; lon += 4) {
        grat.push(...toVector3(lon, lat, GLOBE_RADIUS * 1.0005).toArray());
        grat.push(...toVector3(lon + 4, lat, GLOBE_RADIUS * 1.0005).toArray());
      }
    }
    for (let lon = -180; lon < 180; lon += 15) {
      for (let lat = -88; lat < 88; lat += 4) {
        grat.push(...toVector3(lon, lat, GLOBE_RADIUS * 1.0005).toArray());
        grat.push(...toVector3(lon, lat + 4, GLOBE_RADIUS * 1.0005).toArray());
      }
    }
    const gratGeo = new THREE.BufferGeometry();
    gratGeo.setAttribute("position", new THREE.Float32BufferAttribute(grat, 3));
    s.add(
      new THREE.LineSegments(
        gratGeo,
        new THREE.LineBasicMaterial({ color: 0x18334a, transparent: true, opacity: 0.55 })
      )
    );

    const group = new THREE.Group();
    dataGroup.current = group;
    s.add(group);

    const raycaster = new THREE.Raycaster();
    raycaster.params.Points = { threshold: 0.02 };
    const pointer = new THREE.Vector2();
    let pointerInside = false;
    let lastMove = { x: 0, y: 0 };

    const pick = (): { feature: Feature; screen: { x: number; y: number } } | null => {
      const cloud = markerCloud.current;
      if (!cloud || !pointerInside) return null;
      raycaster.setFromCamera(pointer, cam);
      const hitsFound = raycaster.intersectObject(cloud, false);
      for (const found of hitsFound) {
        const entry = placed.current[found.index ?? -1];
        if (!entry) continue;
        // Reject markers on the far side of the globe: the point cloud has no
        // depth test against the ocean sphere, so without this the pointer
        // picks things hidden behind the planet.
        if (entry.position.clone().normalize().dot(cam.position.clone().normalize()) < 0.06) {
          continue;
        }
        return { feature: entry.feature, screen: lastMove };
      }
      return null;
    };

    const onPointerMove = (event: PointerEvent) => {
      const rect = r.domElement.getBoundingClientRect();
      pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
      pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
      lastMove = { x: event.clientX - rect.left, y: event.clientY - rect.top };
      pointerInside = true;
      const found = pick();
      r.domElement.style.cursor = found ? "pointer" : "grab";
      setTip(found ? { ...found.screen, feature: found.feature } : null);
      handlers.current.onHover(found?.feature.id ?? null);
    };

    const onPointerLeave = () => {
      pointerInside = false;
      setTip(null);
      handlers.current.onHover(null);
    };

    // Distinguish a click from the end of a drag, so rotating the globe does
    // not also clear or change the selection.
    let downAt: { x: number; y: number; t: number } | null = null;
    const onPointerDown = (e: PointerEvent) => {
      downAt = { x: e.clientX, y: e.clientY, t: performance.now() };
    };
    const onPointerUp = (e: PointerEvent) => {
      if (!downAt) return;
      const moved = Math.hypot(e.clientX - downAt.x, e.clientY - downAt.y);
      const quick = performance.now() - downAt.t < 500;
      downAt = null;
      if (moved > 5 || !quick) return;
      const found = pick();
      handlers.current.onSelect(found?.feature.id ?? null);
    };

    r.domElement.addEventListener("pointermove", onPointerMove);
    r.domElement.addEventListener("pointerleave", onPointerLeave);
    r.domElement.addEventListener("pointerdown", onPointerDown);
    r.domElement.addEventListener("pointerup", onPointerUp);
    r.domElement.style.cursor = "grab";

    const onResize = () => {
      if (!mount.clientWidth) return;
      cam.aspect = mount.clientWidth / mount.clientHeight;
      cam.updateProjectionMatrix();
      r.setSize(mount.clientWidth, mount.clientHeight);
    };
    const observer = new ResizeObserver(onResize);
    observer.observe(mount);

    let frame = 0;
    const clock = new THREE.Clock();
    const tick = () => {
      frame = requestAnimationFrame(tick);
      const elapsed = clock.getElapsedTime();

      // Camera flight to a selected feature: ease the position along a great
      // circle so the globe appears to turn rather than the camera to teleport.
      if (flight.current) {
        const f = flight.current;
        f.t = Math.min(1, f.t + 0.028);
        const eased = 1 - Math.pow(1 - f.t, 3);
        const next = new THREE.Vector3().copy(f.from).lerp(f.to, eased).normalize();
        cam.position.copy(next.multiplyScalar(f.from.length() * (1 - eased) + f.to.length() * eased));
        if (f.t >= 1) flight.current = null;
      }

      // Selected and hovered markers breathe, which is what ties a row in the
      // list to a point on the globe when the pointer is on the other one.
      group.traverse((object) => {
        const pulse = object.userData.pulse as number | undefined;
        if (pulse !== undefined) {
          const scale = 1 + Math.sin(elapsed * 3.4) * 0.14 * pulse;
          object.scale.setScalar(scale);
        }
      });

      controls.current.update();
      r.render(s, cam);
    };
    tick();

    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      r.domElement.removeEventListener("pointermove", onPointerMove);
      r.domElement.removeEventListener("pointerleave", onPointerLeave);
      r.domElement.removeEventListener("pointerdown", onPointerDown);
      r.domElement.removeEventListener("pointerup", onPointerUp);
      c.dispose();
      r.dispose();
      s.traverse((object) => {
        const mesh = object as THREE.Mesh;
        mesh.geometry?.dispose?.();
        const material = mesh.material as THREE.Material | THREE.Material[] | undefined;
        if (Array.isArray(material)) material.forEach((m) => m.dispose());
        else material?.dispose?.();
      });
      if (r.domElement.parentNode === mount) mount.removeChild(r.domElement);
    };
  }, []);

  /* --- coastlines and borders -------------------------------------------- */
  useEffect(() => {
    if (!geo || !scene.current) return;
    const layers: THREE.Object3D[] = [];

    const addLines = (lines: [number, number][][], color: number, opacity: number, lift: number) => {
      const points: number[] = [];
      for (const line of lines) {
        for (let i = 0; i < line.length - 1; i += 1) {
          points.push(...toVector3(line[i][0], line[i][1], GLOBE_RADIUS * lift).toArray());
          points.push(...toVector3(line[i + 1][0], line[i + 1][1], GLOBE_RADIUS * lift).toArray());
        }
      }
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(points, 3));
      const object = new THREE.LineSegments(
        geometry,
        new THREE.LineBasicMaterial({ color, transparent: true, opacity })
      );
      scene.current.add(object);
      layers.push(object);
    };

    addLines(geo.land, 0x3f6d93, 0.95, 1.001);
    addLines(geo.nation, 0x69a6d4, 1.0, 1.0015);
    addLines(geo.states, 0x3a6183, 0.85, 1.0012);

    return () => {
      for (const layer of layers) {
        scene.current.remove(layer);
        const line = layer as THREE.LineSegments;
        line.geometry.dispose();
        (line.material as THREE.Material).dispose();
      }
    };
  }, [geo]);

  /* --- data layer: polygons and markers ---------------------------------- */
  useEffect(() => {
    const group = dataGroup.current;
    if (!group) return;

    while (group.children.length) {
      const child = group.children.pop()!;
      const mesh = child as THREE.Mesh;
      mesh.geometry?.dispose?.();
      const material = mesh.material as THREE.Material | undefined;
      material?.dispose?.();
    }
    markerCloud.current = null;

    const hitById = new Map(hits.map((h) => [h.id, h]));
    const entries: Placed[] = [];
    const positions: number[] = [];
    const colors: number[] = [];
    const sizes: number[] = [];
    const outline: number[] = [];
    const outlineColors: number[] = [];

    for (const feature of features) {
      const anchor = anchorOf(feature);
      if (!anchor) continue;
      const color = new THREE.Color(colorOf(feature));
      const isHit = hitById.has(feature.id);
      const position = toVector3(anchor.lon, anchor.lat, GLOBE_RADIUS * 1.004);

      entries.push({ feature, position });
      positions.push(position.x, position.y, position.z);
      colors.push(color.r, color.g, color.b);
      sizes.push(isHit ? 9 : feature.source_type === "alert" ? 7 : 4.5);

      // The published warning footprint, where there is one.
      if (feature.geometry) {
        for (const ring of feature.geometry.rings) {
          for (let i = 0; i < ring.length - 1; i += 1) {
            outline.push(...toVector3(ring[i][0], ring[i][1], GLOBE_RADIUS * 1.003).toArray());
            outline.push(...toVector3(ring[i + 1][0], ring[i + 1][1], GLOBE_RADIUS * 1.003).toArray());
            const intensity = isHit ? 1 : 0.55;
            outlineColors.push(
              color.r * intensity, color.g * intensity, color.b * intensity,
              color.r * intensity, color.g * intensity, color.b * intensity
            );
          }
        }
      }
    }

    if (outline.length) {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(outline, 3));
      geometry.setAttribute("color", new THREE.Float32BufferAttribute(outlineColors, 3));
      group.add(
        new THREE.LineSegments(
          geometry,
          new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.9 })
        )
      );
    }

    if (entries.length) {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
      geometry.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
      geometry.setAttribute("size", new THREE.Float32BufferAttribute(sizes, 1));

      // A shader rather than PointsMaterial so each marker can carry its own
      // size and still shrink with distance, and so the disc is drawn with a
      // soft edge instead of a hard square.
      const material = new THREE.ShaderMaterial({
        transparent: true,
        depthWrite: false,
        uniforms: { uScale: { value: window.innerHeight / 2 } },
        vertexShader: `
          attribute float size;
          varying vec3 vColor;
          uniform float uScale;
          void main() {
            vColor = color;
            vec4 mv = modelViewMatrix * vec4(position, 1.0);
            gl_PointSize = size * (uScale / -mv.z) * 0.012;
            gl_Position = projectionMatrix * mv;
          }`,
        fragmentShader: `
          varying vec3 vColor;
          void main() {
            float d = length(gl_PointCoord - vec2(0.5));
            if (d > 0.5) discard;
            float alpha = smoothstep(0.5, 0.16, d);
            gl_FragColor = vec4(vColor, alpha);
          }`,
        vertexColors: true,
      });
      const cloud = new THREE.Points(geometry, material);
      group.add(cloud);
      markerCloud.current = cloud;
    }

    placed.current = entries;
  }, [features, hits]);

  /* --- search results: relevance as altitude ----------------------------- */
  useEffect(() => {
    const group = dataGroup.current;
    if (!group) return;

    const columns = group.children.filter((child) => child.userData.isColumn);
    for (const column of columns) {
      group.remove(column);
      const mesh = column as THREE.Mesh;
      mesh.geometry?.dispose?.();
      (mesh.material as THREE.Material)?.dispose?.();
    }
    if (!hits.length) return;

    // Most documents carry the coordinate of the city they were fetched for,
    // so several hits routinely land on the exact same point. Fanning them
    // around that point keeps every result visible as its own bar instead of
    // stacking them into one.
    const byAnchor = new Map<string, SearchHit[]>();
    for (const hit of hits) {
      const anchor = anchorOf(hit);
      if (!anchor) continue;
      const key = `${anchor.lon.toFixed(2)},${anchor.lat.toFixed(2)}`;
      (byAnchor.get(key) ?? byAnchor.set(key, []).get(key)!).push(hit);
    }

    for (const group_ of byAnchor.values()) {
      group_.forEach((hit, index) => {
        const anchor = anchorOf(hit)!;
        const spread = group_.length > 1 ? 0.9 : 0;
        const angle = (index / Math.max(1, group_.length)) * Math.PI * 2;
        const lon = anchor.lon + Math.cos(angle) * spread;
        const lat = anchor.lat + Math.sin(angle) * spread * 0.72;

        const height = columnHeight(hit.similarity);
        const base = toVector3(lon, lat, GLOBE_RADIUS * 1.002);
        const color = new THREE.Color(colorOf(hit));

        const beam = new THREE.Mesh(
          new THREE.CylinderGeometry(0.0042, 0.0042, height, 6, 1, true),
          new THREE.MeshBasicMaterial({
            color,
            transparent: true,
            opacity: 0.82,
            depthWrite: false,
          })
        );
        // Cylinders are built along +Y; align to the surface normal so the beam
        // stands up out of the globe wherever it is.
        beam.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), base.clone().normalize());
        beam.position.copy(base.clone().normalize().multiplyScalar(GLOBE_RADIUS * 1.002 + height / 2));
        beam.userData.isColumn = true;
        beam.userData.id = hit.id;
        group.add(beam);

        const cap = new THREE.Mesh(
          new THREE.SphereGeometry(0.013, 12, 10),
          new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.95 })
        );
        cap.position.copy(
          base.clone().normalize().multiplyScalar(GLOBE_RADIUS * 1.002 + height)
        );
        cap.userData.isColumn = true;
        cap.userData.id = hit.id;
        cap.userData.pulse = 0;
        group.add(cap);
      });
    }
  }, [hits]);

  /* --- highlight the active feature -------------------------------------- */
  useEffect(() => {
    const group = dataGroup.current;
    if (!group) return;
    const active = selectedId ?? hoveredId;
    group.traverse((object) => {
      if (object.userData.isColumn && object.userData.pulse !== undefined) {
        object.userData.pulse = object.userData.id === active ? 1 : 0;
        if (object.userData.id !== active) object.scale.setScalar(1);
      }
    });
  }, [selectedId, hoveredId, hits]);

  /* --- fly to the selected feature --------------------------------------- */
  const flyTo = useCallback((feature: Feature) => {
    const anchor = anchorOf(feature);
    if (!anchor || !camera.current) return;
    const distance = Math.max(1.8, Math.min(camera.current.position.length(), 2.5));
    flight.current = {
      from: camera.current.position.clone(),
      // Offset south of the target so the arrival view stays oblique and the
      // columns keep reading as heights rather than as dots.
      to: toVector3(anchor.lon, Math.max(-60, anchor.lat - 13), distance),
      t: 0,
    };
  }, []);

  const featureById = useMemo(() => {
    const map = new Map<string, Feature>();
    for (const feature of features) map.set(feature.id, feature);
    for (const hit of hits) map.set(hit.id, hit);
    return map;
  }, [features, hits]);

  useEffect(() => {
    if (!selectedId) return;
    const feature = featureById.get(selectedId);
    if (feature) flyTo(feature);
  }, [selectedId, featureById, flyTo]);

  return (
    <div className="globe-layer" ref={mountRef}>
      {tip && (
        <div className="tip" style={{ left: tip.x, top: tip.y }}>
          <b>{tip.feature.event ?? tip.feature.location}</b>
          <span>
            {tip.feature.location}
            {tip.feature.severity ? ` · ${tip.feature.severity}` : ""}
          </span>
        </div>
      )}
    </div>
  );
}
