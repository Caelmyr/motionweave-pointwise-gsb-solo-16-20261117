"""Source-time feature tracking for mask motion.

A user outlines a polygon on one source frame; the tracker follows the textured
patch through the neighbouring frames and turns the result into ordinary mask
keyframes (position, rotation and scale via a similarity transform).  Every
emitted keyframe remains a plain, hand-editable mask keyframe, so manual
corrections become anchors for the next tracking pass.

Frames that cannot be matched (occlusion, texture loss) are reported as
unreliable and tracking stops at that frame instead of silently drifting onto
the background: no mask keyframe is produced past the failure until the user
adds (or corrects) an anchor and tracks again.
"""
import cv2
import numpy as np
from .model import Invalid

# Working images are downscaled so that their longest side is at most this many
# pixels. All geometry is scaled back to source-normalized coordinates.
WORK_LONG_SIDE = 480.0
RATIO = 0.75
RANSAC_THRESHOLD = 2.5
MIN_GOOD_MATCHES = 8
MIN_INLIERS = 6
# Inlier counts below these thresholds mark the frame marginal / failed.
MARGINAL_INLIERS = 14
GOOD_ERROR = 1.2
MARGINAL_ERROR = 3.0


def gray_and_scale(rgba):
    """Return an uint8 grayscale image, downscaled on its longest side."""
    rgb = rgba[..., :3].astype(np.float32)
    alpha = rgba[..., 3].astype(np.float32) / 255.0
    # Composite transparent pixels over mid-gray so alpha edges keep contrast.
    gray = (rgb * alpha[..., None] + 128.0 * (1.0 - alpha[..., None])).mean(axis=2)
    gray = np.clip(gray, 0, 255).astype(np.uint8)
    height, width = gray.shape
    factor = min(1.0, WORK_LONG_SIDE / max(width, height))
    if factor < 1.0:
        gray = cv2.resize(gray, (max(1, round(width * factor)),
                                 max(1, round(height * factor))),
                          interpolation=cv2.INTER_AREA)
    return gray


def roi_mask(shape, polygon_norm):
    mask = np.zeros(shape, np.uint8)
    points = np.rint(np.asarray(polygon_norm, dtype=np.float64)
                     * [shape[1], shape[0]]).astype(np.int32)
    cv2.fillPoly(mask, [points], 255)
    return mask


def detect(orb, gray, polygon_norm):
    """ORB features whose keypoints fall inside the tracked polygon."""
    keypoints, descriptors = orb.detectAndCompute(gray, roi_mask(gray.shape, polygon_norm))
    return keypoints, descriptors


def match_estimate(bf, kp_a, des_a, kp_b, des_b):
    """Estimate the similarity transform mapping frame A points to frame B.

    Returns (matrix_2x3, inlier_count, good_match_count, mean_reprojection)
    or None when the frame pair cannot be tracked reliably.
    """
    if des_a is None or des_b is None or len(kp_a) < MIN_GOOD_MATCHES or len(kp_b) < 2:
        return None
    pairs = bf.knnMatch(des_a, des_b, k=2)
    good = [m for pair in pairs if len(pair) == 2
            for m, n in [pair] if m.distance < RATIO * n.distance]
    if len(good) < MIN_GOOD_MATCHES:
        return None
    pts_a = np.float64([kp_a[m.queryIdx].pt for m in good])
    pts_b = np.float64([kp_b[m.trainIdx].pt for m in good])
    matrix, inliers = cv2.estimateAffinePartial2D(
        pts_a, pts_b, method=cv2.RANSAC, ransacReprojThreshold=RANSAC_THRESHOLD,
        maxIters=3000, confidence=0.99)
    if matrix is None or inliers is None:
        return None
    inliers = inliers.ravel().astype(bool)
    count = int(inliers.sum())
    if count < MIN_INLIERS:
        return None
    projected = pts_a @ matrix[:, :2].T + matrix[:, 2]
    error = float(np.linalg.norm(projected - pts_b, axis=1)[inliers].mean())
    return matrix, count, len(good), error


def grade(inliers, good_matches, error):
    """Three-level reliability: reliable / marginal / failed."""
    if inliers < MIN_INLIERS or error > MARGINAL_ERROR:
        return 'failed'
    if inliers < MARGINAL_INLIERS or error > GOOD_ERROR:
        return 'marginal'
    return 'reliable'


def score(inliers, good_matches, error):
    """0..1 confidence used on the emitted mask keyframe."""
    strength = min(1.0, inliers / 30.0)
    agreement = min(1.0, good_matches / max(1, inliers)) * (1.0 / (1.0 + max(0.0, error - 0.5)))
    return round(max(0.0, min(1.0, 0.6 * strength + 0.4 * agreement)), 4)


def identity_matrix():
    return np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float64)


def apply_matrix(polygon_norm, matrix, from_shape, to_shape):
    """Apply a working-pixel affine matrix to normalized polygon points."""
    from_h, from_w = from_shape
    to_h, to_w = to_shape
    points = np.asarray(polygon_norm, dtype=np.float64) * [from_w, from_h]
    moved = points @ matrix[:, :2].T + matrix[:, 2]
    return (moved / [to_w, to_h]).tolist()


def frame_times(frames):
    """Left-closed start time of every source frame."""
    times, offset = [], 0.0
    for frame in frames:
        times.append(offset)
        offset += float(frame['duration'])
    return times


def track(frames, images, mask_keys, first=0, last=None):
    """Track anchor polygons across source frames.

    ``mask_keys`` are the clip's current mask keyframes. Manual keyframes inside
    the frame window are the anchors; if the window has no manual keyframe (for
    example a split child), its existing tracked keyframes seed the pass.
    Tracking proceeds outward from each anchor in both frame directions and
    stops at the first failed pair, leaving an explicit gap in the reliability
    report instead of producing drifting keyframes.

    Returns ``(keyframes, report)`` where keyframes are full source-time mask
    keyframes (anchors preserved, tracked frames added) and report holds one
    entry per source frame in ``[first, last]``.
    """
    if not mask_keys:
        raise Invalid('请先在一帧上圈定遮罩区域作为跟踪起点')
    vertex_count = len(mask_keys[0]['points'])
    if any(len(key['points']) != vertex_count for key in mask_keys):
        raise Invalid('同一遮罩各关键帧顶点对应关系必须一致')
    last = len(frames) - 1 if last is None else int(last)
    if not 0 <= first <= last < len(frames):
        raise Invalid('跟踪帧范围不合法')

    times = frame_times(frames)
    in_window = []
    for key in mask_keys:
        index = _frame_index(times, float(key['time']))
        if first <= index <= last:
            in_window.append((index, key))
    # Manual keyframes are authoritative user anchors. When the window has none
    # (e.g. a split child whose only manual anchor sits outside its window), the
    # existing tracked keyframes seed a fresh pass instead. Out-of-window
    # keyframes are preserved untouched by the store's merge.
    manual = [(i, k) for i, k in in_window if k.get('origin', 'manual') == 'manual']
    seeds = manual if manual else in_window
    anchors = []
    used = set()
    for index, key in seeds:
        if index in used:
            raise Invalid('同一源帧上存在多个遮罩锚点')
        used.add(index)
        anchors.append((index, key))
    if not anchors:
        raise Invalid('跟踪范围内没有遮罩锚点，请先在范围内的源帧上圈定区域')

    grays = [gray_and_scale(image) for image in images]
    orb = cv2.ORB_create(nfeatures=900, fastThreshold=12)
    bf = cv2.BFMatcher(cv2.NORM_HAMMING)

    report = [{'frame': index, 'time': times[index], 'state': 'untracked',
               'confidence': None, 'inliers': 0, 'matches': 0,
               'error': None, 'anchor': index in used}
              for index in range(first, last + 1)]
    by_frame = {row['frame']: row for row in report}
    for index, key in anchors:
        by_frame[index].update(state='anchor', confidence=float(key.get('confidence', 1.0) or 1.0),
                               inliers=key.get('inliers', 0), matches=key.get('matches', 0),
                               error=key.get('error'))

    # Output starts from the in-window anchors (manual anchors are preserved).
    outputs = {index: {'time': times[index], 'points': [list(p) for p in key['points']],
                       'origin': key.get('origin', 'manual'),
                       'confidence': float(key.get('confidence', 1.0) or 1.0),
                       'inliers': key.get('inliers', 0), 'matches': key.get('matches', 0),
                       'error': key.get('error')}
               for index, key in anchors}
    anchor_set = set(used)

    def run_segment(anchor_index, anchor_key, direction):
        """Walk away from one anchor in +1/-1 direction until a failure."""
        current = anchor_index
        cumulative = identity_matrix()
        current_polygon = [list(p) for p in anchor_key['points']]
        while True:
            nxt = current + direction
            if nxt < first or nxt > last or nxt in anchor_set:
                return
            if nxt in outputs:
                # Another anchor's segment already claimed this frame; keep its
                # keyframe and reliability row untouched.
                return
            kp_a, des_a = detect(orb, grays[current], current_polygon)
            # The candidate polygon is unknown until we estimate the step, so
            # search the whole next frame for candidate features.
            kp_b, des_b = orb.detectAndCompute(grays[nxt], None)
            estimate = match_estimate(bf, kp_a, des_a, kp_b, des_b)
            row = by_frame[nxt]
            row['matches'] = 0 if estimate is None else estimate[2]
            if estimate is None:
                row.update(state='failed', confidence=0.0, inliers=0, error=None)
                return
            step, inliers, good_matches, error = estimate
            state = grade(inliers, good_matches, error)
            row.update(state=state, confidence=score(inliers, good_matches, error),
                       inliers=inliers, error=round(error, 4))
            if state == 'failed':
                return
            cumulative = step @ np.vstack([cumulative, [0.0, 0.0, 1.0]])
            cumulative = cumulative[:2]
            polygon = apply_matrix(anchor_key['points'], cumulative,
                                   grays[anchor_index].shape, grays[nxt].shape)
            outputs[nxt] = {'time': times[nxt], 'points': polygon, 'origin': 'track',
                            'confidence': row['confidence'], 'inliers': inliers,
                            'matches': good_matches, 'error': row['error']}
            current_polygon = polygon
            current = nxt

    for anchor_index, anchor_key in sorted(anchors):
        run_segment(anchor_index, anchor_key, +1)
        run_segment(anchor_index, anchor_key, -1)

    keyframes = [outputs[index] for index in sorted(outputs) if first <= index <= last]
    return keyframes, report


def _frame_index(times, time):
    """Source frame whose left-closed interval contains ``time``."""
    index = 0
    for candidate, start in enumerate(times):
        if start <= time + 1e-9:
            index = candidate
        else:
            break
    return index


def clip_frame_window(src, item):
    """Source frame indices covered by a clip's in/out window."""
    times = frame_times(src['frames'])
    first = _frame_index(times, float(item['in']))
    last = _frame_index(times, min(float(item['out']) - 1e-9, src['duration'] - 1e-9))
    return first, last


def load_images(store_assets, frames):
    """Decode source frame assets from the asset directory."""
    from .render import decode
    images = []
    for frame in frames:
        images.append(decode((store_assets / (frame['asset'] + '.png')).read_bytes()))
    return images


def source_signature(src):
    """Fingerprint of the current frame assets; changes when a frame is replaced."""
    import hashlib
    return hashlib.sha256('|'.join(f['asset'] for f in src['frames']).encode()).hexdigest()


def tracking_is_stale(src, record):
    """True when source frames changed after the tracking record was produced."""
    if not record or not record.get('source_signature'):
        return False
    return record['source_signature'] != source_signature(src)
