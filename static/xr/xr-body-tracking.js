/* Three tracked points cannot uniquely determine a torso. Conservative heuristic:
   use coherent two-hand world motion as evidence of a body turn; otherwise keep
   the torso still and let the neck/head move. This is not measured chest tracking. */
import * as THREE from 'three';
const Y = new THREE.Vector3(0, 1, 0);
const FORWARD = new THREE.Vector3(0, 0, -1);
export const wrapAngle = angle => Math.atan2(Math.sin(angle), Math.cos(angle));
export const rounded = n => Math.round(n * 1e5) / 1e5;
export function yawOfQuaternion(q, fallback = 0) {
  const f = FORWARD.clone().applyQuaternion(q);
  return Math.hypot(f.x, f.z) > .12 ? Math.atan2(-f.x, -f.z) : fallback;
}
function yawDelta(a, b) {
  const q = b.clone().multiply(a.clone().invert());
  return wrapAngle(2 * Math.atan2(q.y, q.w)); // world-Y twist, independent of palm pitch/roll
}
function validVector(a, n) {return Array.isArray(a) && a.length === n && a.every(Number.isFinite);}
function pairOf(hands) {
  const p = {};
  for (const h of hands || []) {
    if ((h.handedness === 'left' || h.handedness === 'right') && validVector(h.p, 3)) {
      const q = validVector(h.q, 4) ? new THREE.Quaternion().fromArray(h.q) : null;
      p[h.handedness] = {p: new THREE.Vector3().fromArray(h.p), q: q && q.lengthSq() > 1e-8 ? q.normalize() : null};
    }
  }
  if (!p.left || !p.right) return null;
  const span = p.right.p.clone().sub(p.left.p); span.y = 0;
  if (span.length() < .16) return null; // crossed/overlapping hands don't define a useful yaw
  return {...p, span, center: p.left.p.clone().add(p.right.p).multiplyScalar(.5)};
}

export class BodyTracker {
  reset() {this.last = null; this.history = []; this.confidence = 0;}
  constructor() {this.reset();}
  sample(headPosition, headQuaternion, hands, time) {
    if (headQuaternion.lengthSq() < 1e-8) return null;
    const head = headPosition.clone(), q = headQuaternion.clone().normalize();
    if (![head.x,head.y,head.z,q.x,q.y,q.z,q.w,time].every(Number.isFinite)) return null;
    const previous = this.last;
    if (!previous && (head.y < .3 || head.y > 12)) return null; // await a real floor-relative HMD pose
    if (previous && time <= previous.time) return this.state(previous);
    const headYaw = yawOfQuaternion(q, previous?.headYaw || 0);
    const pair = pairOf(hands);
    let yaw = previous?.bodyYaw ?? headYaw;
    let anchor = previous?.anchor.clone() || head.clone();
    const neutralY = previous?.neutralY ?? head.y;
    const dt = previous ? time - previous.time : 0;
    if (dt > 1.5 || (previous && head.distanceTo(previous.head) > 1.2)) {
      // Resume/recenter: don't compare a stale controller pose to a new reference space.
      this.reset(); return this.sample(headPosition, headQuaternion, hands, time);
    }
    let coherent = false;
    if (previous) {
      if (!pair || !previous.pair) {this.history = []; this.confidence = 0;}
      const candidates = this.history.filter(s => time - s.time >= .15 && time - s.time <= .65 && s.pair);
      const ref = pair && candidates[0];
      if (ref && ref.pair) {
        const dh = wrapAngle(headYaw - ref.headYaw);
        const dp = Math.atan2(ref.pair.span.z * pair.span.x - ref.pair.span.x * pair.span.z,
          ref.pair.span.dot(pair.span));
        const sizeRatio = pair.span.length() / ref.pair.span.length();
        const enoughArc = Math.abs(dh) > .025 && ref.pair.span.length() * Math.abs(dp) > .012;
        const positional = enoughArc && sizeRatio > .8 && sizeRatio < 1.25
          && dp * dh > 0 && Math.abs(dp - dh) < Math.max(.06, Math.abs(dh) * .4);
        // Rotating both wrists in place is not a body turn: span rotation is REQUIRED.
        const orientation = ['left','right'].every(side => {
          const a = ref.pair[side].q, b = pair[side].q;
          if (!a || !b) return false;
          const d = yawDelta(a,b);
          return d * dp > 0 && Math.abs(d) > Math.abs(dp) * .4
            && Math.abs(d - dp) < Math.max(.06, Math.abs(dp) * .4);
        });
        coherent = positional && orientation;
        if (coherent) {
          this.confidence = Math.min(1, this.confidence + dt * 8);
          if (this.confidence > .45) yaw = wrapAngle(ref.bodyYaw + dp);
        } else this.confidence = Math.max(0,this.confidence-dt*12);
      } else this.confidence = 0;
      // Without two reliable hands, hold body yaw, with an anatomical safety limit.
      // Never immediately turn the body to match the HMD on a tracking dropout.
      if (!pair) {
        const gap = wrapAngle(headYaw - yaw);
        if (Math.abs(gap) > 1.5) yaw = wrapAngle(yaw + Math.sign(gap) * Math.min(Math.abs(gap)-1.5, dt*.6));
      }
      const bodyStep = wrapAngle(yaw - previous.bodyYaw);
      const turn = new THREE.Quaternion().setFromAxisAngle(Y, bodyStep);
      const headShift = head.clone().sub(previous.head);
      if (pair && previous.pair) {
        const predictedCenter = previous.pair.center.clone().sub(previous.anchor).applyQuaternion(turn).add(previous.anchor);
        const handShift = pair.center.clone().sub(predictedCenter); handShift.y = 0;
        const predictedHead = previous.head.clone().sub(previous.anchor).applyQuaternion(turn).add(previous.anchor);
        const residualHead = head.clone().sub(predictedHead); residualHead.y = 0;
        if (handShift.distanceTo(residualHead) < .055 && handShift.length() > .002
            && residualHead.length() > .002 && handShift.dot(residualHead) > 0) {
          anchor.add(handShift.clone().add(residualHead).multiplyScalar(.5));
        }
      } else {anchor.x += headShift.x; anchor.z += headShift.z;}
      // Bounded lean: beyond 30cm allow the lower body to move; it cannot stay
      // planted meters behind a walking user if hands are waving independently.
      const lean = head.clone().sub(anchor); lean.y = 0;
      if (lean.length() > .3) anchor.addScaledVector(lean, 1 - .3 / lean.length());
    }
    this.history = this.history.filter(s => time - s.time <= .65);
    const current = {head,q,headYaw,pair,bodyYaw:yaw,anchor,neutralY,time,coherent};
    this.history.push(current);this.last=current;
    return this.state(current);
  }
  state(s) {
    // Compact enough for the existing binary state block's 255-byte limit.
    return {v:1,p:s.head.toArray().map(rounded),q:s.q.toArray().map(rounded),
      b:[s.bodyYaw,s.anchor.x,s.anchor.z].map(rounded),h:rounded(s.neutralY)};
  }
}

export function readBodyTracking(state, pose) {
  const s = state?.xr;
  if (s?.v !== 1 || !validVector(s.p,3) || !validVector(s.q,4) || !validVector(s.b,3) || !Number.isFinite(s.h) || s.h < .3 || s.h > 12) return null;
  if (s.b.slice(1).some(v => Math.abs(v) > 40)) return null;
  const q = new THREE.Quaternion().fromArray(s.q);
  if (q.lengthSq() < 1e-8) return null;
  // Sparse distant LOD omits state. Don't pin a moving person to an old torso
  // anchor or use a stale head quaternion carried by the merged previous state.
  if(pose){
    if(!validVector(pose.p,3)||Math.hypot(...s.p.map((x,i)=>x-pose.p[i]))>.025)return null;
    const e=new THREE.Euler().setFromQuaternion(q.clone().normalize(),'YXZ');
    if(Number.isFinite(pose.yaw)&&Math.abs(wrapAngle(e.y-pose.yaw))>.03)return null;
    if(Number.isFinite(pose.pitch)&&Math.abs(e.x-pose.pitch)>.03)return null;
  }
  return {q:q.normalize(), yaw:wrapAngle(s.b[0]), x:s.b[1], z:s.b[2], neutralY:s.h};
}

// Viewer uses -Z forward/+Y up, but VRM0 and VRM1 model fronts differ.
// Capture this semantic frame in each bone's local bind coordinates, just as
// with the palms. This must not change the existing body facing correction.
export function measureHeadFrame(vrm, node) {
  if (!node) return null;
  node.updateWorldMatrix(true, false);
  const inv = node.getWorldQuaternion(new THREE.Quaternion()).invert();
  const root = vrm.scene.getWorldQuaternion(new THREE.Quaternion());
  const forward = new THREE.Vector3(0,0,vrm.meta.metaVersion === '0' ? -1 : 1).applyQuaternion(root).applyQuaternion(inv);
  const up = new THREE.Vector3(0,1,0).applyQuaternion(root).applyQuaternion(inv);
  const right = new THREE.Vector3().crossVectors(forward,up).normalize();
  up.crossVectors(right,forward).normalize();
  const boneToViewer = new THREE.Quaternion().setFromRotationMatrix(new THREE.Matrix4().makeBasis(right,up,forward.clone().negate())).invert();
  return {forward,up,boneToViewer};
}
