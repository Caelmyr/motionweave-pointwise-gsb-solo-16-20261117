import io
import math
from flask import jsonify, render_template, request, send_file
from .model import Invalid, Conflict, number, identity, find
from .store import Store
from .render import png, render
from .timeline import duration, inspect
from .delivery import capture, bundle


def install(app, root):
    store = Store(root)
    app.extensions['motionweave'] = store

    @app.errorhandler(Invalid)
    def invalid(error):
        return jsonify({'error': str(error)}), 409 if isinstance(error, Conflict) else 400

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise Invalid('需要JSON对象')
        return value

    @app.get('/')
    def home():
        return render_template('motion.html')

    @app.get('/motion/health')
    def health():
        return jsonify({'ok': True, 'product': 'MotionWeave'})

    @app.get('/motion/projects')
    def projects():
        return jsonify(store.list())

    @app.post('/motion/projects')
    def create():
        data = body()
        return jsonify(store.create(data.get('name', '未命名动画'), data.get('width', 640), data.get('height', 360), data.get('fps', 12)))

    @app.post('/motion/demo')
    def demo():
        return jsonify(store.demo())

    @app.get('/motion/projects/<key>')
    def project(key):
        return jsonify(store.get(key))

    @app.patch('/motion/projects/<key>')
    def update(key):
        data = body()
        revision = data.pop('revision')
        return jsonify(store.update(key, revision, data))

    @app.post('/motion/projects/<key>/sources')
    def sources(key):
        files = request.files.getlist('files')
        if not files or len(files) > 120:
            raise Invalid('请选择1至120张图像')
        seconds = number(request.form.get('duration', .25), '帧时长', .01, 60)
        frames = []
        for file in files:
            frame = store.asset(file.read())
            frame['duration'] = seconds
            frames.append(frame)
        return jsonify(store.add_source(key, int(request.form['revision']), request.form.get('name', '导入序列'), frames))

    @app.post('/motion/projects/<key>/sources/<source_id>/frames/<int:index>')
    def replace(key, source_id, index):
        if 'file' not in request.files:
            raise Invalid('请选择替换图像')
        frame = store.asset(request.files['file'].read())
        return jsonify(store.replace_frame(key, int(request.form['revision']), source_id, index, frame))

    @app.post('/motion/projects/<key>/clips')
    def clips(key):
        data = body()
        return jsonify(store.add_clip(key, data['revision'], data['source'], data.get('start', 0), data.get('lane', 0)))

    @app.post('/motion/projects/<key>/clips/<clip_id>/<action>')
    def action(key, clip_id, action):
        data = body()
        if action == 'track':
            return jsonify(store.track_clip(
                key, data.pop('revision'), clip_id,
                data.get('first'), data.get('last')))
        return jsonify(store.clip_action(key, data.pop('revision'), clip_id, action, data))

    @app.post('/motion/projects/<key>/<action>')
    def history(key, action):
        if action not in ['undo', 'redo']:
            raise Invalid('未知操作')
        return jsonify(store.travel(key, body()['revision'], action == 'redo'))

    @app.get('/motion/projects/<key>/preview.png')
    def preview(key):
        doc = store.get(key)
        time = number(request.args.get('time', 0), '时间', 0, max(duration(doc), 0))
        return send_file(io.BytesIO(png(render(doc, time, store.assets))), mimetype='image/png', max_age=0)

    @app.get('/motion/projects/<key>/inspect')
    def inspect_frame(key):
        doc = store.get(key)
        time = number(request.args.get('time', 0), '时间', 0, max(duration(doc), 0))
        return jsonify({'time': time, 'layers': inspect(doc, time), 'duration': duration(doc)})

    @app.post('/motion/projects/<key>/captures')
    def captures(key):
        data = body()
        return jsonify(capture(store, key, data['revision'], data.get('start', 0), data.get('end')))

    @app.get('/motion/projects/<key>/captures/<capture_id>/<name>')
    def captured(key, capture_id, name):
        if name == 'bundle.zip':
            return send_file(io.BytesIO(bundle(store, key, capture_id)), mimetype='application/zip', as_attachment=True, download_name='motionweave-'+capture_id[:8]+'.zip')
        folder, _, _ = store.captured(key, capture_id)
        if name != 'contact.png' and not (len(name) == 9 and name[:5].isdigit() and name.endswith('.png')):
            raise Invalid('不支持该文件')
        return send_file(folder / name, mimetype='image/png')

    @app.get('/motion/assets/<asset>.png')
    def asset(asset):
        if len(asset) != 64 or any(c not in '0123456789abcdef' for c in asset):
            raise Invalid('无效资源')
        return send_file(store.assets / (asset + '.png'), mimetype='image/png')
