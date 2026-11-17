import copy
import math
import uuid
from datetime import datetime, timezone


class Invalid(ValueError):
    pass


class Conflict(Invalid):
    pass


def uid():
    return uuid.uuid4().hex


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def number(value, name, low=None, high=None):
    if isinstance(value, bool):
        raise Invalid(name + '必须是数值')
    try:
        value = float(value)
    except (ValueError, TypeError):
        raise Invalid(name + '必须是数值')
    if not math.isfinite(value):
        raise Invalid(name + '必须有限')
    if low is not None and value < low or high is not None and value > high:
        raise Invalid(name + '超出范围')
    return value


def integer(value, name, low, high):
    result = number(value, name, low, high)
    if result != int(result):
        raise Invalid(name + '必须是整数')
    return int(result)


def identity(value):
    if not isinstance(value, str) or len(value) != 32 or any(c not in '0123456789abcdef' for c in value):
        raise Invalid('无效标识')
    return value


def document(name, width, height, fps):
    return {'id': uid(), 'name': str(name)[:100], 'revision': 0,
            'created': timestamp(), 'width': integer(width, '宽', 16, 1920),
            'height': integer(height, '高', 16, 1080), 'fps': number(fps, '帧率', 1, 60),
            'background': [0.035, 0.05, 0.08, 1], 'sources': [], 'clips': [],
            'captures': [], 'undo': [], 'redo': []}


def source(name, frames):
    if not frames or len(frames) > 120:
        raise Invalid('序列需要1至120张帧')
    out = {'id': uid(), 'name': str(name)[:100], 'frames': frames}
    for frame in frames:
        frame['duration'] = number(frame['duration'], '源帧时长', 0.01, 60)
    out['duration'] = sum(frame['duration'] for frame in frames)
    return out


def find(items, key):
    for item in items:
        if item['id'] == key:
            return item
    raise Invalid('对象不存在')


def clip(source_id, duration, start=0, lane=0):
    return {'id': uid(), 'source': source_id, 'start': float(start),
            'length': float(duration), 'in': 0., 'out': float(duration),
            'reverse': False, 'lane': int(lane), 'enabled': True,
            'blend': 'normal', 'opacity': 1.,
            'transform': [{'time': 0., 'x': 0., 'y': 0., 'scale': 1., 'angle': 0.}],
            'mask': [], 'feather': 0.}


def validate_transform(keys):
    if not isinstance(keys, list) or not keys or len(keys) > 100:
        raise Invalid('运动关键帧数量不合法')
    times = set()
    result = []
    for key in keys:
        time = number(key['time'], '源时间', 0, 7200)
        if time in times:
            raise Invalid('运动关键帧时间重复')
        times.add(time)
        result.append({'time': time, 'x': number(key.get('x', 0), '横移', -4000, 4000),
                       'y': number(key.get('y', 0), '纵移', -4000, 4000),
                       'scale': number(key.get('scale', 1), '缩放', .01, 20),
                       'angle': number(key.get('angle', 0), '旋转', -36000, 36000)})
    return sorted(result, key=lambda item: item['time'])


def validate_masks(keys):
    if not isinstance(keys, list) or len(keys) > 100:
        raise Invalid('遮罩关键帧数量不合法')
    result = []
    times = set()
    vertices = None
    for key in keys:
        time = number(key['time'], '源时间', 0, 7200)
        if time in times:
            raise Invalid('遮罩关键帧时间重复')
        times.add(time)
        points = key['points']
        if not 3 <= len(points) <= 64:
            raise Invalid('遮罩需3至64个顶点')
        if vertices is not None and vertices != len(points):
            raise Invalid('同一遮罩各关键帧顶点对应关系必须一致')
        vertices = len(points)
        result.append({'time': time, 'points': [[number(p[0], '遮罩X', -4, 4), number(p[1], '遮罩Y', -4, 4)] for p in points]})
    return sorted(result, key=lambda item: item['time'])


def validate(doc):
    if len(doc['clips']) > 48 or len(doc['sources']) > 24:
        raise Invalid('工程最多24序列、48片段')
    ids = set()
    for item in doc['clips']:
        if item['id'] in ids:
            raise Invalid('片段标识重复')
        ids.add(item['id'])
        src = find(doc['sources'], item['source'])
        item['start'] = number(item['start'], '开始时间', 0, 7200)
        item['length'] = number(item['length'], '合成时长', .01, 7200)
        item['in'] = number(item['in'], '入点', 0, src['duration'])
        item['out'] = number(item['out'], '出点', 0, src['duration'])
        if item['out'] <= item['in']:
            raise Invalid('出点必须晚于入点')
        item['lane'] = integer(item['lane'], '轨道', 0, 15)
        item['opacity'] = number(item['opacity'], '不透明度', 0, 1)
        item['feather'] = number(item['feather'], '羽化', 0, 64)
        if item['blend'] not in ['normal', 'multiply', 'screen']:
            raise Invalid('未知混合方式')
        item['transform'] = validate_transform(item['transform'])
        item['mask'] = validate_masks(item['mask'])
    duration = max([c['start'] + c['length'] for c in doc['clips']] or [0])
    if duration * doc['fps'] > 3600:
        raise Invalid('工程最多3600输出帧')
    return doc


def content(doc):
    return {key: copy.deepcopy(value) for key, value in doc.items()
            if key not in ['revision', 'undo', 'redo', 'captures']}
