"""End-to-end tests for source-time mask tracking.

Run with:  python3 -I tests/test_tracking.py
Uses only the standard library unittest; synthetic frames are generated in a
temp directory and never touch real footage.
"""
import copy
import json
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from motionweave.model import validate, Invalid
from motionweave import tracker
from motionweave.store import Store
from motionweave.render import png, render
from motionweave.timeline import source_time, frame_at


def make_sequence(store, count=10, size=(180, 240), shift=(10, 5),
                  occlusion_from=None, occlusion_to=None, textureless_from=None):
    """A textured square moving by a fixed step each frame."""
    rng = np.random.default_rng(42)
    texture = rng.integers(15, 250, (72, 72), dtype=np.uint8)
    # High-frequency checker and edges give the small patch dense ORB features.
    checker = np.indices((72, 72)).sum(axis=0) % 8 < 4
    texture[checker] = np.clip(texture[checker].astype(int) + 90, 0, 255)
    texture[::5, :] = 12
    texture[:, ::5] = 244
    frames = []
    for index in range(count):
        image = np.zeros((size[0], size[1], 4), dtype=np.uint8)
        image[..., :3] = [18, 26, 38]
        image[..., 3] = 255
        if textureless_from is not None and index >= textureless_from:
            pass  # leave a flat, featureless field
        else:
            x = 18 + index * shift[0]
            y = 30 + index * shift[1]
            region = image[y:y + 72, x:x + 72]
            if region.shape[:2] == (72, 72):
                image[y:y + 72, x:x + 72, 0] = texture
                image[y:y + 72, x:x + 72, 1] = np.roll(texture, 7, axis=1)
                image[y:y + 72, x:x + 72, 2] = np.roll(texture, 13, axis=0)
            if (occlusion_from is not None and occlusion_from <= index
                    and (occlusion_to is None or index <= occlusion_to)):
                # Blank out the tracked object with a flat occluder.
                image[y:y + 72, x:x + 72, :3] = [200, 200, 200]
        frame = store.asset(png(image))
        frame['duration'] = 0.25
        frames.append(frame)
    return frames


def square_polygon(x0, y0, side=72, size=(180, 240)):
    return [[x0 / size[1], y0 / size[0]],
            [(x0 + side) / size[1], y0 / size[0]],
            [(x0 + side) / size[1], (y0 + side) / size[0]],
            [x0 / size[1], (y0 + side) / size[0]]]


def centroid(points):
    return np.asarray(points).mean(axis=0)


class TrackerUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, 'data'))

    def tearDown(self):
        self.tmp.cleanup()

    def test_polygon_follows_translation(self):
        frames = make_sequence(self.store, count=8)
        images = tracker.load_images(self.store.assets, frames)
        anchor = {'time': 0.0, 'points': square_polygon(18, 30)}
        keyframes, report = tracker.track(frames, images, [anchor], 0, 7)
        self.assertEqual(len(keyframes), 8)
        states = {row['frame']: row['state'] for row in report}
        self.assertEqual(states[0], 'anchor')
        # The moving textured patch should be reliable over the whole run.
        self.assertTrue(all(states[i] == 'reliable' for i in range(1, 8)), states)
        # Tracked centroid must follow the known 10px/frame horizontal step.
        centroids = [centroid(k['points']) for k in keyframes]
        drift_x = [(centroids[i + 1][0] - centroids[i][0]) * 240 for i in range(7)]
        self.assertTrue(all(abs(d - 10.0) < 2.5 for d in drift_x), drift_x)
        # Polygon area must stay roughly constant (no scale drift).
        areas = [polygon_area(k['points']) for k in keyframes]
        self.assertTrue(all(abs(a / areas[0] - 1) < 0.08 for a in areas), areas)

    def test_occlusion_marks_failure_and_stops_instead_of_drifting(self):
        frames = make_sequence(self.store, count=10, occlusion_from=6, occlusion_to=9)
        images = tracker.load_images(self.store.assets, frames)
        anchor = {'time': 0.0, 'points': square_polygon(18, 30)}
        keyframes, report = tracker.track(frames, images, [anchor], 0, 9)
        states = {row['frame']: row['state'] for row in report}
        # Frames before the occluder are tracked; the occluded frame fails and
        # no keyframe is emitted past it.
        self.assertIn(states[6], ('failed', 'marginal'))
        emitted = sorted(
            tracker._frame_index(tracker.frame_times(frames), k['time']) for k in keyframes)
        self.assertNotIn(7, emitted)
        self.assertNotIn(8, emitted)
        self.assertTrue(all(row['state'] != 'reliable' or row['frame'] < 6
                            for row in report if row['frame'] >= 6))

    def test_textureless_region_is_unreliable(self):
        frames = make_sequence(self.store, count=8, textureless_from=5)
        images = tracker.load_images(self.store.assets, frames)
        anchor = {'time': 0.0, 'points': square_polygon(18, 30)}
        keyframes, report = tracker.track(frames, images, [anchor], 0, 7)
        states = {row['frame']: row['state'] for row in report}
        self.assertIn(states[5], ('failed', 'marginal'))
        # Failure must not be hidden as a confident, quietly drifting keyframe.
        bad = [r for r in report if r['frame'] >= 5 and r['state'] == 'reliable']
        self.assertEqual(bad, [])

    def test_manual_anchor_reconnects_tracking_after_gap(self):
        frames = make_sequence(self.store, count=10, occlusion_from=5, occlusion_to=6)
        images = tracker.load_images(self.store.assets, frames)
        # First pass from frame 0 dies at the temporary occlusion.
        first_anchor = {'time': 0.0, 'points': square_polygon(18, 30)}
        keyframes, report = tracker.track(frames, images, [first_anchor], 0, 9)
        self.assertIn({r['frame']: r['state'] for r in report}[5], ('failed', 'marginal'))
        # User manually corrects the polygon onto the post-occlusion object at
        # frame 8 (object has moved to x=18+8*10=98, y=30+8*5=70).
        manual = {'time': tracker.frame_times(frames)[8],
                  'points': square_polygon(98, 70), 'origin': 'manual'}
        keyframes2, report2 = tracker.track(frames, images,
                                            [first_anchor, manual], 0, 9)
        states = {row['frame']: row['state'] for row in report2}
        self.assertEqual(states[8], 'anchor')
        # The new anchor drives tracking of the tail frames.
        self.assertIn(states[9], ('reliable', 'marginal', 'anchor'))
        emitted = {tracker._frame_index(tracker.frame_times(frames), k['time']) for k in keyframes2}
        self.assertIn(9, emitted)
        # The manual anchor polygon is preserved verbatim.
        at8 = next(k for k in keyframes2 if k['time'] == manual['time'])
        np.testing.assert_allclose(at8['points'], manual['points'])
        self.assertEqual(at8['origin'], 'manual')

    def test_multiple_anchors_do_not_overwrite_each_other(self):
        frames = make_sequence(self.store, count=10)
        images = tracker.load_images(self.store.assets, frames)
        times = tracker.frame_times(frames)
        anchors = [{'time': times[1], 'points': square_polygon(18 + 10, 30 + 5)},
                   {'time': times[8], 'points': square_polygon(18 + 80, 30 + 40)}]
        keyframes, report = tracker.track(frames, images, anchors, 0, 9)
        states = {r['frame']: r['state'] for r in report}
        self.assertEqual(states[1], 'anchor')
        self.assertEqual(states[8], 'anchor')
        # Every frame covered by at least one anchor's segment is resolved once.
        emitted_frames = {tracker._frame_index(times, k['time']) for k in keyframes}
        self.assertEqual(len(emitted_frames), len(keyframes))
        # Frames between two anchors must not remain untracked.
        self.assertTrue(all(states[i] != 'untracked' for i in range(1, 9)), states)
        # Anchor polygons preserved verbatim.
        at1 = next(k for k in keyframes if k['time'] == times[1])
        np.testing.assert_allclose(at1['points'], anchors[0]['points'])

    def test_bidirectional_tracking_from_middle_anchor(self):
        frames = make_sequence(self.store, count=8)
        images = tracker.load_images(self.store.assets, frames)
        # Anchor on frame 4: object at x=18+40=58, y=30+20=50.
        anchor = {'time': tracker.frame_times(frames)[4],
                  'points': square_polygon(58, 50)}
        keyframes, report = tracker.track(frames, images, [anchor], 0, 7)
        emitted = {tracker._frame_index(tracker.frame_times(frames), k['time']) for k in keyframes}
        self.assertEqual(emitted, set(range(8)))
        centroids = {tracker._frame_index(tracker.frame_times(frames), k['time']):
                     centroid(k['points']) for k in keyframes}
        # Backward direction must recover the earlier positions too.
        delta = (centroids[3] - centroids[4]) * [240, 180]
        self.assertAlmostEqual(delta[0], -10.0, delta=2.0)
        self.assertAlmostEqual(delta[1], -5.0, delta=2.0)

    def test_requires_anchor(self):
        frames = make_sequence(self.store, count=4)
        images = tracker.load_images(self.store.assets, frames)
        with self.assertRaises(Invalid):
            tracker.track(frames, images, [], 0, 3)

    def test_rotation_and_scale_are_recovered(self):
        import cv2
        rng = np.random.default_rng(7)
        organic = cv2.GaussianBlur(rng.integers(0, 255, (150, 150), np.uint8), (7, 7), 2.5)
        size = (220, 220)
        frames, images = [], []
        specs = [(0.0, 1.0), (10.0, 1.06), (-7.0, 0.93)]
        for angle, scale in specs:
            matrix = cv2.getRotationMatrix2D((75, 75), angle, scale)
            matrix[:, 2] += [110 - 75, 110 - 75]
            warped = cv2.warpAffine(organic, matrix, size, borderValue=128)
            rgba = np.dstack([warped] * 3 + [np.full(size, 255, np.uint8)])
            images.append(rgba)
            frame = self.store.asset(png(rgba))
            frame['duration'] = 0.25
            frames.append(frame)
        # Polygon around the texture block (centered at canvas center).
        anchor = {'time': 0.0, 'points': [[35 / 220, 35 / 220], [185 / 220, 35 / 220],
                                          [185 / 220, 185 / 220], [35 / 220, 185 / 220]]}
        keyframes, report = tracker.track(frames, images, [anchor], 0, 2)
        self.assertTrue(all(r['state'] in ('reliable', 'anchor') for r in report),
                        [r['state'] for r in report])
        # Recovered similarity parameters from the emitted polygons.
        for index, (angle, scale) in enumerate(specs[1:], start=1):
            base = np.asarray(keyframes[0]['points'])
            moved = np.asarray(keyframes[index]['points'])
            # Similarity fit between the two normalized polygons.
            sim, _ = cv2.estimateAffinePartial2D(base, moved)
            got_angle = math.degrees(math.atan2(sim[1, 0], sim[0, 0]))
            got_scale = math.hypot(sim[0, 0], sim[1, 0])
            # getRotationMatrix2D uses the image y-down convention; the polygon
            # similarity fit reports the opposite sign, so compare magnitudes.
            self.assertAlmostEqual(abs(got_angle), abs(angle), delta=2.0)
            self.assertAlmostEqual(got_scale, scale, delta=0.06)


def polygon_area(points):
    pts = np.asarray(points)
    x, y = pts[:, 0], pts[:, 1]
    return 0.5 * abs(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1)))


class StoreLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, 'data'))
        self.doc = self.store.create('跟踪工程', 480, 270, 8)
        frames = make_sequence(self.store, count=10)
        self.doc = self.store.add_source(self.doc['id'], self.doc['revision'], '运动序列', frames)
        self.source = self.doc['sources'][0]
        self.doc = self.store.add_clip(self.doc['id'], self.doc['revision'], self.source['id'])
        self.clip_id = self.doc['clips'][0]['id']
        anchor = {'time': 0.0, 'points': square_polygon(18, 30)}
        self.doc = self.store.clip_action(
            self.doc['id'], self.doc['revision'], self.clip_id, 'edit',
            {'mask': [anchor], 'feather': 1.5})

    def tearDown(self):
        self.tmp.cleanup()

    def clip(self, doc=None):
        doc = doc or self.doc
        return next(c for c in doc['clips'] if c['id'] == self.clip_id)

    def test_track_endpoint_writes_keyframes_and_report(self):
        doc = self.store.track_clip(self.doc['id'], self.doc['revision'], self.clip_id)
        item = next(c for c in doc['clips'] if c['id'] == self.clip_id)
        self.assertGreaterEqual(len(item['mask']), 9)
        self.assertTrue(all('confidence' in k for k in item['mask']))
        self.assertEqual(len(item['tracking']['frames']), 10)
        self.assertEqual(item['tracking']['source_signature'].__len__(), 64)
        # Persisted document must validate and reload identically apart from
        # the read-time staleness annotation (not written to disk).
        reloaded = self.store.get(doc['id'])
        self.assertEqual(reloaded['clips'][0]['tracking']['stale'], False)
        validate(copy.deepcopy(reloaded))
        on_disk = json.loads((self.store.projects / doc['id'] / 'project.json').read_text())
        self.assertNotIn('stale', on_disk['clips'][0]['tracking'])
        persisted = {k: v for k, v in item['tracking'].items() if k != 'stale'}
        self.assertEqual(on_disk['clips'][0]['tracking'], persisted)

    def test_trim_speed_reverse_and_split_keep_mask_on_source_object(self):
        doc = self.store.track_clip(self.doc['id'], self.doc['revision'], self.clip_id)
        rev = doc['revision']
        item = next(c for c in doc['clips'] if c['id'] == self.clip_id)
        # Trim the in/out points to frames 2..7 and retime (speed change).
        doc = self.store.clip_action(doc['id'], rev, self.clip_id, 'edit', {
            'in': 0.5, 'out': 2.0, 'length': 1.0})
        # Reverse playback.
        doc = self.store.clip_action(doc['id'], doc['revision'], self.clip_id, 'edit',
                                     {'reverse': True})
        item = next(c for c in doc['clips'] if c['id'] == self.clip_id)
        # Mask at a reversed timeline time resolves to the same source frame's
        # mask as forward playback of that source time.
        mid = item['start'] + item['length'] / 2
        source_seconds = source_time(item, mid)
        index, _ = frame_at(self.source, source_seconds)
        # Find the mask keyframe emitted for that source frame.
        times = tracker.frame_times(self.source['frames'])
        key = min(item['mask'], key=lambda k: abs(k['time'] - times[index]))
        self.assertLess(abs(key['time'] - times[index]), 0.26)
        # Split the clip: both children keep their source-time masks, and the
        # reliability report is windowed to each child.
        right_id = None
        doc = self.store.clip_action(doc['id'], doc['revision'], self.clip_id, 'split',
                                     {'time': item['start'] + item['length'] / 2})
        children = [c for c in doc['clips'] if c['id'] == self.clip_id or
                    (c.get('mask') and c['source'] == self.source['id'])]
        self.assertEqual(len(children), 2)
        left, right = children if children[0]['id'] == self.clip_id else children[::-1]
        right_id = right['id']
        for child in (left, right):
            validate(doc)
            self.assertGreaterEqual(len(child['mask']), 1)
            frames_covered = {row['frame'] for row in child['tracking']['frames']}
            if child['id'] == self.clip_id:
                self.assertTrue(frames_covered <= set(range(0, 5)))
            else:
                self.assertTrue(frames_covered <= set(range(5, 10)))
        # Reopen from disk: masks and tracking survive.
        reloaded = self.store.get(doc['id'])
        ids = {c['id'] for c in reloaded['clips']}
        self.assertIn(right_id, ids)
        for child in reloaded['clips']:
            self.assertTrue(all(len(k['points']) == 4 for k in child['mask']))

    def test_preview_and_capture_use_tracked_masks_consistently(self):
        doc = self.store.track_clip(self.doc['id'], self.doc['revision'], self.clip_id)
        # Preview render at several times must succeed and stay inside canvas.
        for t in (0.0, 0.75, 1.5, 2.25):
            image = render(doc, t, self.store.assets)
            self.assertEqual(image.shape, (270, 480, 4))
        from motionweave.delivery import capture
        result = self.store.list()  # sanity: listing still works
        self.assertTrue(any(p['id'] == doc['id'] for p in result))
        updated = capture(self.store, doc['id'], doc['revision'], 0, 2.0)
        cap = updated['clips'] and next(c for c in updated['captures'])
        self.assertEqual(cap['count'], 16)  # 2 seconds * 8 fps
        # The frozen capture document carries the tracked mask verbatim.
        _, frozen, metadata = self.store.captured(doc['id'], cap['id'])
        frozen_clip = frozen['clips'][0]
        self.assertGreaterEqual(len(frozen_clip['mask']), 9)
        self.assertEqual(metadata['count'], 16)
        # Editing afterwards does not mutate the immutable capture.
        after = self.store.clip_action(updated['id'], updated['revision'], self.clip_id,
                                       'edit', {'mask': []})
        _, frozen2, _ = self.store.captured(doc['id'], cap['id'])
        self.assertGreaterEqual(len(frozen2['clips'][0]['mask']), 9)

    def test_legacy_manual_mask_without_tracking_fields_validates(self):
        doc = self.store.clip_action(
            self.doc['id'], self.doc['revision'], self.clip_id, 'edit',
            {'mask': [{'time': 0.1, 'points': square_polygon(20, 40)}]})
        item = next(c for c in doc['clips'] if c['id'] == self.clip_id)
        self.assertEqual(item['mask'][0]['origin'], 'manual')
        self.assertNotIn('confidence', item['mask'][0])
        validate(self.store.get(doc['id']))

    def test_undo_restores_pre_tracking_state(self):
        before = self.store.get(self.doc['id'])
        tracked = self.store.track_clip(before['id'], before['revision'], self.clip_id)
        undone = self.store.travel(tracked['id'], tracked['revision'])
        item = next(c for c in undone['clips'] if c['id'] == self.clip_id)
        self.assertEqual(len(item['mask']), 1)
        self.assertIsNone(item['tracking'])

    def test_frame_replacement_marks_tracking_stale_then_retracking_clears(self):
        doc = self.store.track_clip(self.doc['id'], self.doc['revision'], self.clip_id)
        item = next(c for c in doc['clips'] if c['id'] == self.clip_id)
        self.assertFalse(item['tracking']['stale'])
        # Replace one source frame with a fresh image.
        rng = np.random.default_rng(99)
        replacement = np.dstack([
            rng.integers(0, 255, (180, 240), np.uint8)] * 3
            + [np.full((180, 240), 255, np.uint8)])
        doc = self.store.replace_frame(
            doc['id'], doc['revision'], self.source['id'], 3,
            self.store.asset(png(replacement)))
        item = next(c for c in doc['clips'] if c['id'] == self.clip_id)
        self.assertTrue(item['tracking']['stale'])
        # The stored record itself keeps no stale flag; it is a read-time annotation.
        on_disk = json.loads((self.store.projects / doc['id'] / 'project.json').read_text())
        self.assertNotIn('stale', on_disk['clips'][0]['tracking'])
        # Re-tracking against the new frames produces a fresh, non-stale record.
        doc = self.store.track_clip(doc['id'], doc['revision'], self.clip_id)
        item = next(c for c in doc['clips'] if c['id'] == self.clip_id)
        self.assertFalse(item['tracking']['stale'])

    def test_split_children_retrack_and_keep_source_time_masks(self):
        doc = self.store.track_clip(self.doc['id'], self.doc['revision'], self.clip_id)
        item = next(c for c in doc['clips'] if c['id'] == self.clip_id)
        mid = item['start'] + item['length'] / 2
        doc = self.store.clip_action(doc['id'], doc['revision'], self.clip_id,
                                     'split', {'time': mid})
        right_id = next(c['id'] for c in doc['clips'] if c['id'] != self.clip_id)
        # Reverse the right child, then re-track it using its inherited tracked
        # keyframes as seeds (it has no manual anchor in its own window).
        doc = self.store.clip_action(doc['id'], doc['revision'], right_id,
                                     'edit', {'reverse': True})
        doc = self.store.track_clip(doc['id'], doc['revision'], right_id)
        right = next(c for c in doc['clips'] if c['id'] == right_id)
        self.assertTrue(right['tracking']['frames'])
        self.assertTrue(all(row['frame'] >= 5 for row in right['tracking']['frames']))
        # Left child with the original manual anchor re-tracks as reliable.
        doc = self.store.track_clip(doc['id'], doc['revision'], self.clip_id)
        left = next(c for c in doc['clips'] if c['id'] == self.clip_id)
        states = {row['state'] for row in left['tracking']['frames']}
        self.assertIn('reliable', states)

    def test_reverse_clip_resolves_same_source_mask(self):
        doc = self.store.track_clip(self.doc['id'], self.doc['revision'], self.clip_id)
        item = next(c for c in doc['clips'] if c['id'] == self.clip_id)
        from motionweave.timeline import mask_at
        times = tracker.frame_times(self.source['frames'])
        # Forward clip at source time t and reversed clip at the timeline time
        # that maps back to t must yield identical mask polygons.
        forward_polygon = mask_at(item, times[4])
        doc = self.store.clip_action(doc['id'], doc['revision'], self.clip_id, 'edit',
                                     {'reverse': True})
        reversed_clip = next(c for c in doc['clips'] if c['id'] == self.clip_id)
        # Timeline time whose reversed mapping lands on source frame 4.
        fraction = (times[4] - reversed_clip['in']) / (reversed_clip['out'] - reversed_clip['in'])
        timeline_time = reversed_clip['start'] + (1.0 - fraction) * reversed_clip['length']
        reversed_polygon = mask_at(reversed_clip, source_time(reversed_clip, timeline_time))
        np.testing.assert_allclose(reversed_polygon, forward_polygon, atol=1e-9)


if __name__ == '__main__':
    unittest.main(verbosity=2)
