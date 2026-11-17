import csv
import io
import json
import shutil
import zipfile
from pathlib import Path
from .model import uid, timestamp, Invalid
from .timeline import schedule, inspect
from .render import render, png, contact_sheet


def capture(store, key, revision, start=0, end=None):
    doc, signature = store.capture_begin(key, revision)
    times = schedule(doc, start, end)
    if not times:
        raise Invalid('没有可输出的帧')
    folder = store.root / 'staging' / uid()
    folder.mkdir(parents=True)
    manifest = []
    thumbs = []
    try:
        for index, time in enumerate(times):
            image = render(doc, time, store.assets)
            name = f'{index:05d}.png'
            (folder / name).write_bytes(png(image))
            manifest.append({'index': index, 'time': time, 'file': name, 'layers': inspect(doc, time)})
            if index % max(1, len(times)//12) == 0 and len(thumbs) < 12:
                thumbs.append(image)
        (folder / 'contact.png').write_bytes(png(contact_sheet(thumbs)))
        store.write(folder / 'frames.json', manifest)
        metadata = {'created': timestamp(), 'count': len(times), 'start': times[0],
                    'end': end if end is not None else max(c['start']+c['length'] for c in doc['clips']),
                    'fps': doc['fps'], 'width': doc['width'], 'height': doc['height']}
        return store.capture_commit(key, doc, signature, folder, metadata)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def timeline_csv(doc, times):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(['output_index', 'timeline_seconds', 'clip', 'source', 'source_seconds', 'source_frame', 'lane'])
    for index, time in enumerate(times):
        layers = inspect(doc, time)
        if not layers:
            writer.writerow([index, time, '', '', '', '', ''])
        for layer in layers:
            writer.writerow([index, time, layer['clip'], layer['source'], layer['source_time'], layer['frame'], layer['lane']])
    return stream.getvalue().encode('utf-8-sig')


def bundle(store, key, capture_id):
    folder, doc, metadata = store.captured(key, capture_id)
    records = store.read(folder / 'frames.json')
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for record in records:
            archive.write(folder / record['file'], 'frames/' + record['file'])
        for name in ['document.json', 'metadata.json', 'frames.json', 'contact.png']:
            archive.write(folder / name, name)
        archive.writestr('timeline.csv', timeline_csv(doc, [r['time'] for r in records]))
    return stream.getvalue()
