/* WebXR hand orientation contract.
   Wire palm-v1: -Z = fingers (wrist -> knuckles), -Y = outward palm normal.
   Joint/wrist spaces already use this frame; grip spaces do NOT.
   Specs: immersive-web.github.io/webxr/#dom-xrinputsource-gripspace
          immersive-web.github.io/webxr-hand-input/#xrjointspace */
import * as THREE from 'three';

export const HAND_ORIENTATION = 'palm-v1';
const PALM = new THREE.Vector3(0, -1, 0);
const IDENTITY = new THREE.Quaternion();

// Canonical palm axes expressed in grip space. +Y points back along the arm;
// extended fingers are -Y. Palm points +X on the left, -X on the right.
const GRIP_FROM_PALM = Object.fromEntries(['left', 'right'].map(side => {
  const s = side === 'left' ? -1 : 1;
  return [side, new THREE.Quaternion().setFromRotationMatrix(new THREE.Matrix4().makeBasis(
    new THREE.Vector3(0, 0, s), new THREE.Vector3(s, 0, 0), new THREE.Vector3(0, 1, 0),
  ))];
}));

export function xrOrientationToPalm(q, space, side, out = new THREE.Quaternion()) {
  if (!q || ![q.x, q.y, q.z, q.w].every(Number.isFinite) || q.lengthSq() < 1e-12) return null;
  if (space === 'grip' && !GRIP_FROM_PALM[side]) return null;
  out.copy(q).normalize();
  if (space === 'grip') out.multiply(GRIP_FROM_PALM[side]);
  // wrist/palm are exact; ray is only a last-resort pointing approximation.
  return out.normalize();
}

export function readXRHands(controllers, grips, hands, sources = []) {
  const result = [];
  for (let i = 0; i < controllers.length; i++) {
    const controller = controllers[i];
    const input = controller.userData.inputSource || sources[i];
    if (!input) continue;
    const side = input.handedness;
    if (side !== 'left' && side !== 'right') continue; // no gaze/screen/phantom second hand
    const wrist = hands[i]?.joints?.wrist;
    let node, space;
    if (input.hand) {
      // Lost hand tracking must not reuse an old wrist or change to a ray frame.
      if (!hands[i]?.visible || !wrist?.visible) continue;
      node = wrist; space = 'wrist';
    } else if (grips[i]?.visible) {
      node = grips[i]; space = 'grip';
    } else if (controller.visible) {
      node = controller; space = 'ray';
    } else continue;
    node.updateWorldMatrix(true, false); // includes rig's translation/snap turn
    const p = node.getWorldPosition(new THREE.Vector3());
    if (![p.x, p.y, p.z].every(Number.isFinite)) continue;
    const q = xrOrientationToPalm(node.getWorldQuaternion(new THREE.Quaternion()), space, side);
    result.push({ handedness: side, p: p.toArray(), q: q ? q.toArray() : null });
  }
  return result;
}

// Measure semantic hand axes in the actual hand bone's LOCAL rest frame.
// No guessing an Euler +/-90deg correction: imported bone rotations and left/right
// mirror anatomy are accounted for using the model's knuckle positions.
export function measureHandFrame(humanoid, side, raw = false) {
  const get = name => raw ? humanoid.getRawBoneNode(name) : humanoid.getNormalizedBoneNode(name);
  const hand = get(`${side}Hand`);
  if (!hand) return null;
  hand.updateWorldMatrix(true, true);
  const wrist = hand.getWorldPosition(new THREE.Vector3());
  const invHand = hand.getWorldQuaternion(new THREE.Quaternion()).invert();
  const vectorTo = name => {
    const node = get(`${side}${name}`);
    return node ? node.getWorldPosition(new THREE.Vector3()).sub(wrist).applyQuaternion(invHand) : null;
  };
  const index = vectorTo('IndexProximal');
  const little = vectorTo('LittleProximal');
  const middle = vectorTo('MiddleProximal');
  let fingers = middle || (index && little ? index.clone().add(little).multiplyScalar(0.5) : index || little);
  let palm;
  if (fingers && fingers.lengthSq() > 1e-10 && index && little) {
    fingers = fingers.clone().normalize();
    const across = index.clone().sub(little);
    palm = new THREE.Vector3().crossVectors(fingers, across).multiplyScalar(side === 'left' ? 1 : -1);
    if (palm.lengthSq() > 1e-10) palm.normalize(); else palm = null;
  }
  if (!fingers || !palm) {
    // Missing/degenerate fingers: VRM normalized T-pose, palms down, arms +/-X.
    // Express these axes in the hand's local frame; don't assume a raw bone basis.
    const rootQ = humanoid.normalizedHumanBonesRoot?.getWorldQuaternion(new THREE.Quaternion()) || IDENTITY;
    fingers = new THREE.Vector3(side === 'left' ? 1 : -1, 0, 0).applyQuaternion(rootQ).applyQuaternion(invHand).normalize();
    palm = PALM.clone().applyQuaternion(rootQ).applyQuaternion(invHand).normalize();
  }
  // Construct an orthonormal basis: canonical Y=-palm, Z=-fingers, X=Y cross Z.
  const z = fingers.clone().negate();
  const x = new THREE.Vector3().crossVectors(palm, fingers).normalize();
  const y = new THREE.Vector3().crossVectors(z, x).normalize();
  palm = y.clone().negate();
  const boneToPalm = new THREE.Quaternion().setFromRotationMatrix(new THREE.Matrix4().makeBasis(x, y, z)).invert();
  return { fingers, palm, boneToPalm };
}

export function handFrameWorld(hand, frame) {
  hand.updateWorldMatrix(true, false);
  const q = hand.getWorldQuaternion(new THREE.Quaternion());
  return { fingers: frame.fingers.clone().applyQuaternion(q).toArray(), palm: frame.palm.clone().applyQuaternion(q).toArray() };
}
