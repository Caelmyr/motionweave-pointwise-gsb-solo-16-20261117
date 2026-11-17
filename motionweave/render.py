import io
from pathlib import Path
import cv2
import numpy as np
from PIL import Image, ImageOps
from .model import Invalid, find
from .timeline import active, source_time, frame_at, matrix_at, mask_at


def decode(data):
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 24000000:
                raise Invalid('图片超过2400万像素')
            result = np.asarray(ImageOps.exif_transpose(image).convert('RGBA')).copy()
    except Invalid:
        raise
    except Exception as error:
        raise Invalid('不能解码图片') from error
    return result


def png(image):
    stream = io.BytesIO()
    Image.fromarray(np.asarray(image, dtype=np.uint8)).save(stream, format='PNG')
    return stream.getvalue()


def linear(rgb):
    return np.where(rgb <= .04045, rgb / 12.92, ((rgb + .055) / 1.055) ** 2.4)


def srgb(rgb):
    return np.where(rgb <= .0031308, rgb * 12.92, 1.055 * np.maximum(rgb, 0) ** (1/2.4) - .055)


def mask_raster(item, time, width, height):
    polygon = mask_at(item, time)
    if polygon is None:
        return np.ones((height, width), dtype=np.float32)
    output = np.zeros((height, width), dtype=np.float32)
    points = np.rint(polygon * [width, height]).astype(np.int32)
    cv2.fillPoly(output, [points], 1.)
    if item['feather'] > 0:
        output = cv2.GaussianBlur(output, (0, 0), item['feather'], borderType=cv2.BORDER_CONSTANT)
    return np.clip(output, 0, 1)


def warp_layer(rgba, item, time, canvas_size):
    height, width = rgba.shape[:2]
    rgb = linear(rgba[..., :3].astype(np.float32) / 255)
    alpha = rgba[..., 3].astype(np.float32) / 255
    alpha *= mask_raster(item, time, width, height)
    alpha *= item['opacity']
    premultiplied = np.dstack([rgb * alpha[..., None], alpha])
    matrix = matrix_at(item, time, (width, height), canvas_size)
    return cv2.warpAffine(premultiplied, matrix, canvas_size,
                          flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)


def composite(back, front, mode):
    source_alpha = front[..., 3:4]
    back_alpha = back[..., 3:4]
    source_rgb = np.divide(front[..., :3], source_alpha, out=np.zeros_like(front[..., :3]), where=source_alpha > 1e-8)
    back_rgb = np.divide(back[..., :3], back_alpha, out=np.zeros_like(back[..., :3]), where=back_alpha > 1e-8)
    if mode == 'multiply':
        blended = source_rgb * back_rgb
    elif mode == 'screen':
        blended = source_rgb + back_rgb - source_rgb * back_rgb
    else:
        blended = source_rgb
    rgb = ((1-source_alpha) * back[..., :3] + (1-back_alpha) * front[..., :3]
           + source_alpha * back_alpha * blended)
    alpha = source_alpha + back_alpha * (1-source_alpha)
    return np.concatenate([rgb, alpha], axis=2)


def render(doc, time, asset_dir):
    width, height = doc['width'], doc['height']
    background = np.asarray(doc['background'], dtype=np.float32)
    canvas = np.empty((height, width, 4), dtype=np.float32)
    canvas[..., :3] = linear(background[:3]) * background[3]
    canvas[..., 3] = background[3]
    cache = {}
    for item in sorted(doc['clips'], key=lambda c: (c['lane'], c['id'])):
        if not active(item, time):
            continue
        src = find(doc['sources'], item['source'])
        seconds = source_time(item, time)
        _, frame = frame_at(src, seconds)
        asset = frame['asset']
        if asset not in cache:
            cache[asset] = decode((Path(asset_dir) / (asset + '.png')).read_bytes())
        layer = warp_layer(cache[asset], item, seconds, (width, height))
        canvas = composite(canvas, layer, item['blend'])
    alpha = canvas[..., 3:4]
    rgb = np.divide(canvas[..., :3], alpha, out=np.zeros_like(canvas[..., :3]), where=alpha > 1e-8)
    output = np.concatenate([srgb(np.clip(rgb, 0, 1)), alpha], axis=2)
    return np.rint(np.clip(output, 0, 1) * 255).astype(np.uint8)


def contact_sheet(images, columns=4, cell_width=200):
    if not images:
        raise Invalid('没有可呈现的帧')
    height, width = images[0].shape[:2]
    cell_height = max(1, round(height * cell_width / width))
    rows = (len(images) + columns - 1) // columns
    sheet = np.zeros((rows*cell_height, columns*cell_width, 4), dtype=np.uint8)
    for index, image in enumerate(images):
        thumb = cv2.resize(image, (cell_width, cell_height), interpolation=cv2.INTER_AREA)
        x = index % columns * cell_width
        y = index // columns * cell_height
        sheet[y:y+cell_height, x:x+cell_width] = thumb
    return sheet
