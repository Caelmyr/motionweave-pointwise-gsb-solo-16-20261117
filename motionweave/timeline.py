import copy
import math
import numpy as np
from .model import Invalid, number, uid, find


def duration(doc):
    return max([c['start'] + c['length'] for c in doc['clips']] or [0.])


def source_time(item, timeline_time):
    fraction = (timeline_time - item['start']) / item['length']
    fraction = min(1., max(0., fraction))
    if item['reverse']:
        fraction = 1. - fraction
    return item['in'] + fraction * (item['out'] - item['in'])


def active(item, time):
    return item['enabled'] and item['start'] <= time < item['start'] + item['length']


def frame_at(src, time):
    offset = 0.
    for index, frame in enumerate(src['frames']):
        end = offset + frame['duration']
        if time < end or index == len(src['frames']) - 1:
            return index, frame
        offset = end
    raise Invalid('空序列')


def bracket(keys, time):
    if time <= keys[0]['time']:
        return keys[0], keys[0], 0.
    for first, second in zip(keys, keys[1:]):
        if time < second['time']:
            weight = (time - first['time']) / (second['time'] - first['time'])
            return first, second, weight
    return keys[-1], keys[-1], 0.


def transform_at(item, source_seconds):
    first, second, weight = bracket(item['transform'], source_seconds)
    angle_delta = (second['angle'] - first['angle'] + 180) % 360 - 180
    return {'x': first['x'] * (1-weight) + second['x'] * weight,
            'y': first['y'] * (1-weight) + second['y'] * weight,
            'scale': math.exp(math.log(first['scale']) * (1-weight) + math.log(second['scale']) * weight),
            'angle': first['angle'] + angle_delta * weight}


def mask_at(item, source_seconds):
    if not item['mask']:
        return None
    first, second, weight = bracket(item['mask'], source_seconds)
    return np.asarray(first['points']) * (1-weight) + np.asarray(second['points']) * weight


def matrix_at(item, source_seconds, source_size, canvas_size):
    state = transform_at(item, source_seconds)
    width, height = source_size
    canvas_width, canvas_height = canvas_size
    angle = math.radians(state['angle'])
    fit = min(canvas_width / width, canvas_height / height) * state['scale']
    cosine, sine = math.cos(angle) * fit, math.sin(angle) * fit
    cx = canvas_width / 2 + state['x']
    cy = canvas_height / 2 + state['y']
    return np.array([[cosine, -sine, cx - cosine*width/2 + sine*height/2],
                     [sine, cosine, cy - sine*width/2 - cosine*height/2]], dtype=np.float64)


def _trim_tracking(item, first=None, last=None):
    """Keep only reliability rows for source frames the clip still covers."""
    record = item.get('tracking')
    if not record or not record.get('frames'):
        return
    src_frames = record['frames']
    if first is None:
        first = min(row['frame'] for row in src_frames)
    if last is None:
        last = max(row['frame'] for row in src_frames)
    record['frames'] = [row for row in src_frames if first <= row['frame'] <= last]
    if 'first' in record:
        record['first'] = max(record['first'], first)
    if 'last' in record:
        record['last'] = min(record['last'], last)


def split(doc, clip_id, time):
    item = find(doc['clips'], clip_id)
    time = number(time, '切割时间', item['start'], item['start'] + item['length'])
    offset = time - item['start']
    if offset < .01 or item['length'] - offset < .01:
        raise Invalid('切割点太靠近片段边缘')
    cut = source_time(item, time)
    right = copy.deepcopy(item)
    right['id'] = uid()
    right['start'] = time
    right['length'] = item['length'] - offset
    item['length'] = offset
    if item['reverse']:
        item['in'] = cut
        right['out'] = cut
    else:
        item['out'] = cut
        right['in'] = cut
    src = find(doc['sources'], item['source'])
    cut_index = frame_at(src, cut)[0]
    # Mask keyframes are shared source-time data; only the reliability report
    # is windowed to the frames each resulting clip still covers.
    _trim_tracking(item, last=cut_index - 1)
    _trim_tracking(right, first=cut_index)
    doc['clips'].append(right)
    return right['id']


def duplicate(doc, clip_id, start, lane):
    item = copy.deepcopy(find(doc['clips'], clip_id))
    item['id'] = uid()
    item['start'] = float(start)
    item['lane'] = int(lane)
    doc['clips'].append(item)
    return item['id']


def edit_clip(doc, clip_id, values):
    item = find(doc['clips'], clip_id)
    allowed = {'start', 'length', 'in', 'out', 'reverse', 'lane', 'enabled', 'blend', 'opacity', 'feather', 'transform', 'mask'}
    if set(values) - allowed:
        raise Invalid('未知片段字段')
    item.update(copy.deepcopy(values))


def ripple_delete(doc, clip_id):
    item = find(doc['clips'], clip_id)
    start, end, lane = item['start'], item['start'] + item['length'], item['lane']
    peers = [c for c in doc['clips'] if c['id'] != clip_id and c['lane'] == lane]
    for peer in peers:
        if peer['start'] < end and peer['start'] + peer['length'] > start:
            raise Invalid('重叠片段不能波纹删除；请普通删除或先移开重叠片段')
    doc['clips'].remove(item)
    for peer in peers:
        if peer['start'] >= end:
            peer['start'] -= item['length']


def schedule(doc, start=0, end=None, fps=None):
    end = duration(doc) if end is None else float(end)
    fps = doc['fps'] if fps is None else float(fps)
    start = number(start, '起点', 0, duration(doc))
    if end <= start or end > duration(doc) + 1e-8:
        raise Invalid('输出区间不合法')
    count = int(math.ceil((end-start) * fps - 1e-9))
    if count > 3600:
        raise Invalid('输出超过3600帧')
    return [start + index / fps for index in range(count)]


def inspect(doc, time):
    rows = []
    for item in sorted(doc['clips'], key=lambda c: (c['lane'], c['id'])):
        if not active(item, time):
            continue
        src = find(doc['sources'], item['source'])
        seconds = source_time(item, time)
        index, frame = frame_at(src, seconds)
        rows.append({'clip': item['id'], 'source': src['id'], 'frame': index,
                     'source_time': seconds, 'asset': frame['asset'], 'lane': item['lane'],
                     'transform': transform_at(item, seconds)})
    return rows
