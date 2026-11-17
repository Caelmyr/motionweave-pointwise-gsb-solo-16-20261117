import copy
import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
import numpy as np
from . import model
from .model import Invalid, Conflict, find
from .render import decode, png
from .timeline import split, duplicate, ripple_delete, edit_clip
from . import tracker


def fingerprint(doc):
    return hashlib.sha256(json.dumps(model.content(doc), sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def fingerprint_without_annotations(doc):
    """Fingerprint ignoring read-only tracking.stale UI annotations."""
    clean = copy.deepcopy(doc)
    for item in clean.get('clips', []):
        record = item.get('tracking')
        if record and 'stale' in record:
            del record['stale']
    return fingerprint(clean)


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.projects = self.root / 'projects'
        self.assets = self.root / 'assets'
        self.lock = RLock()
        self.projects.mkdir(parents=True, exist_ok=True)
        self.assets.mkdir(parents=True, exist_ok=True)

    def write(self, path, value):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.' + model.uid() + '.tmp')
        try:
            with temporary.open('w') as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def read(self, path):
        try:
            return json.loads(Path(path).read_text())
        except (OSError, ValueError) as error:
            raise Invalid('工程记录不可读') from error

    def directory(self, key):
        model.identity(key)
        directory = self.projects / key
        if not (directory / 'project.json').exists():
            raise Invalid('工程不存在')
        return directory

    def get(self, key):
        with self.lock:
            doc = self.read(self.directory(key) / 'project.json')
            self._annotate_stale(doc)
            return doc

    def list(self):
        with self.lock:
            docs = sorted([self.read(p) for p in self.projects.glob('*/project.json')],
                          key=lambda item: item['created'], reverse=True)
            for doc in docs:
                self._annotate_stale(doc)
            return docs

    def _annotate_stale(self, doc):
        """Flag tracking records whose source frames were replaced afterwards."""
        for item in doc.get('clips', []):
            record = item.get('tracking')
            if not record:
                continue
            try:
                src = find(doc['sources'], item['source'])
                record['stale'] = tracker.tracking_is_stale(src, record)
            except Invalid:
                record['stale'] = False

    def create(self, name='未命名动画', width=640, height=360, fps=12):
        doc = model.document(name, width, height, fps)
        with self.lock:
            self.write(self.projects / doc['id'] / 'project.json', doc)
        return doc

    def asset(self, data):
        rgba = decode(data)
        data = png(rgba)
        key = hashlib.sha256(data).hexdigest()
        path = self.assets / (key + '.png')
        if not path.exists():
            temporary = self.assets / (model.uid() + '.tmp')
            temporary.write_bytes(data)
            os.replace(temporary, path)
        return {'asset': key, 'width': rgba.shape[1], 'height': rgba.shape[0], 'duration': .25}

    @contextmanager
    def edit(self, key, revision, operation):
        with self.lock:
            before = self.get(key)
            if before['revision'] != revision:
                raise Conflict('工程已更新，请刷新')
            working = copy.deepcopy(before)
            yield working
            model.validate(working)
            # validate() rebuilds records and drops the read-only stale flag.
            history_id = model.uid()
            entry = {'id': history_id, 'operation': operation,
                     'before': model.content(before), 'after': model.content(working), 'time': model.timestamp()}
            self.write(self.directory(key) / 'history' / (history_id + '.json'), entry)
            working['revision'] = before['revision'] + 1
            working['undo'] = (before['undo'] + [history_id])[-80:]
            working['redo'] = []
            self.write(self.directory(key) / 'project.json', working)

    def add_source(self, key, revision, name, frames):
        src = model.source(name, frames)
        with self.edit(key, revision, '导入图像序列') as doc:
            doc['sources'].append(src)
        return self.get(key)

    def replace_frame(self, key, revision, source_id, index, frame):
        with self.edit(key, revision, '替换源帧') as doc:
            src = find(doc['sources'], source_id)
            index = model.integer(index, '帧序号', 0, len(src['frames'])-1)
            frame['duration'] = src['frames'][index]['duration']
            src['frames'][index] = frame
        return self.get(key)

    def add_clip(self, key, revision, source_id, start=0, lane=0):
        with self.edit(key, revision, '添加片段') as doc:
            src = find(doc['sources'], source_id)
            doc['clips'].append(model.clip(source_id, src['duration'], start, lane))
        return self.get(key)

    def update(self, key, revision, values):
        if set(values) - {'name', 'width', 'height', 'fps', 'background'}:
            raise Invalid('未知工程字段')
        with self.edit(key, revision, '合成设置') as doc:
            if 'name' in values:
                doc['name'] = str(values['name'])[:100]
            for field, low, high in [('width', 16, 1920), ('height', 16, 1080)]:
                if field in values:
                    doc[field] = model.integer(values[field], field, low, high)
            if 'fps' in values:
                doc['fps'] = model.number(values['fps'], '帧率', 1, 60)
            if 'background' in values:
                if len(values['background']) != 4:
                    raise Invalid('背景需RGBA四通道')
                doc['background'] = [model.number(v, '背景', 0, 1) for v in values['background']]
        return self.get(key)

    def clip_action(self, key, revision, clip_id, action, values):
        with self.edit(key, revision, '片段' + action) as doc:
            if action == 'edit':
                edit_clip(doc, clip_id, values)
            elif action == 'split':
                split(doc, clip_id, values['time'])
            elif action == 'duplicate':
                duplicate(doc, clip_id, values['start'], values['lane'])
            elif action == 'ripple-delete':
                ripple_delete(doc, clip_id)
            elif action == 'delete':
                doc['clips'].remove(find(doc['clips'], clip_id))
            else:
                raise Invalid('未知片段操作')
        return self.get(key)

    def track_clip(self, key, revision, clip_id, first=None, last=None):
        with self.lock:
            doc = self.get(key)
            if doc['revision'] != revision:
                raise Conflict('工程已更新，请刷新')
            item = find(doc['clips'], clip_id)
            src = find(doc['sources'], item['source'])
            window_first, window_last = tracker.clip_frame_window(src, item)
            first = window_first if first is None else model.integer(first, '起始帧', 0, len(src['frames']) - 1)
            last = window_last if last is None else model.integer(last, '结束帧', 0, len(src['frames']) - 1)
            if last < first:
                raise Invalid('跟踪结束帧不能早于起始帧')
            if not (window_first <= first and last <= window_last):
                raise Invalid('跟踪范围必须落在片段入出点覆盖的源帧内')
            if not item['mask']:
                raise Invalid('请先在一帧上圈定遮罩区域作为跟踪起点')
            images = tracker.load_images(self.assets, src['frames'])
            keyframes, report = tracker.track(src['frames'], images, item['mask'], first, last)
            signature = tracker.source_signature(src)
            with self.edit(key, revision, '遮罩跟踪') as working:
                target = find(working['clips'], clip_id)
                source_times = tracker.frame_times(src['frames'])
                # Merge: replace keyframes inside the window, keep anything outside.
                outside = [k for k in target['mask']
                           if not (first <= tracker._frame_index(source_times, k['time']) <= last)]
                target['mask'] = sorted(outside + keyframes, key=lambda k: k['time'])
                target['tracking'] = {'frames': report,
                                      'source_revision': working['revision'],
                                      'source_signature': signature,
                                      'tracked_at': model.timestamp(),
                                      'first': first, 'last': last}
        return self.get(key)

    def travel(self, key, revision, redo=False):
        with self.lock:
            current = self.get(key)
            if current['revision'] != revision:
                raise Conflict('工程已更新')
            from_key, to_key = ('redo', 'undo') if redo else ('undo', 'redo')
            if not current[from_key]:
                raise Invalid('没有可恢复的操作')
            history_id = current[from_key][-1]
            history = self.read(self.directory(key) / 'history' / (history_id + '.json'))
            restored = copy.deepcopy(history['after' if redo else 'before'])
            restored['revision'] = current['revision'] + 1
            restored['captures'] = current['captures']
            restored[from_key] = current[from_key][:-1]
            restored[to_key] = current[to_key] + [history_id]
            model.validate(restored)
            self.write(self.directory(key) / 'project.json', restored)
        return self.get(key)

    def capture_begin(self, key, revision):
        with self.lock:
            doc = self.get(key)
            if doc['revision'] != revision:
                raise Conflict('工程已更新')
            frozen = copy.deepcopy(doc)
            # The stale flag is a read-time UI annotation, not document content.
            for item in frozen.get('clips', []):
                if item.get('tracking') and 'stale' in item['tracking']:
                    del item['tracking']['stale']
            return frozen, fingerprint_without_annotations(frozen)

    def capture_commit(self, key, captured, signature, folder, metadata):
        with self.lock:
            current = self.get(key)
            if fingerprint_without_annotations(current) != signature:
                shutil.rmtree(folder, ignore_errors=True)
                raise Conflict('渲染期间工程内容改变，未发布旧结果')
            capture_id = Path(folder).name
            target = self.directory(key) / 'captures' / capture_id
            target.parent.mkdir(parents=True, exist_ok=True)
            self.write(Path(folder) / 'document.json', model.content(captured))
            self.write(Path(folder) / 'metadata.json', metadata)
            os.replace(folder, target)
            current['captures'].append({'id': capture_id, 'revision': captured['revision'],
                                        'signature': signature, **metadata})
            current['revision'] += 1
            self.write(self.directory(key) / 'project.json', current)
        return self.get(key)

    def captured(self, key, capture_id):
        model.identity(capture_id)
        doc = self.get(key)
        find(doc['captures'], capture_id)
        folder = self.directory(key) / 'captures' / capture_id
        return folder, self.read(folder / 'document.json'), self.read(folder / 'metadata.json')

    def demo(self):
        doc = self.create('合成演示 · 轨道与倒放', 480, 270, 8)
        # Deterministic texture that moves with the orange block so feature
        # tracking has corners to lock onto.
        rng = np.random.default_rng(11)
        block_texture = rng.integers(0, 90, (60, 32, 3), dtype=np.uint8)
        block_texture[::4, :] = 0
        frames = []
        for index in range(8):
            image = np.zeros((180, 240, 4), dtype=np.uint8)
            image[..., :3] = [34, 65, 91]
            image[..., 3] = 255
            x = 20 + index*23
            image[40:100, x:x+32, :3] = np.clip(
                np.array([255, 178, 50], dtype=np.uint8) - block_texture, 0, 255)
            image[110:150, 40:200, :3] = [44, 190, 160]
            frame = self.asset(png(image))
            frame['duration'] = .25 if index % 2 == 0 else .5
            frames.append(frame)
        doc = self.add_source(doc['id'], doc['revision'], '合成非等时序列', frames)
        src = doc['sources'][0]
        doc = self.add_clip(doc['id'], doc['revision'], src['id'])
        item = doc['clips'][0]
        # Tight mask around the moving textured block (source image is 240x180).
        def block_mask(x):
            return [[(x - 4) / 240, 36 / 180], [(x + 36) / 240, 36 / 180],
                    [(x + 36) / 240, 104 / 180], [(x - 4) / 240, 104 / 180]]
        doc = self.clip_action(doc['id'], doc['revision'], item['id'], 'edit', {
            'length': 4., 'transform': [{'time': 0., 'x': -25., 'y': 0., 'scale': .8, 'angle': -8.},
                                     {'time': 3., 'x': 25., 'y': 0., 'scale': 1., 'angle': 12.}],
            'mask': [{'time': 0., 'points': block_mask(20)},
                     {'time': 3., 'points': block_mask(20 + 7 * 23)}], 'feather': 2.})
        return doc
