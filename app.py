import os
from image_lab import create_app
from motionweave import install
from waitress import serve
root = os.environ.get('MOTIONWEAVE_DATA_DIR', 'data')
app = create_app(os.path.join(root, 'preparation'))
install(app, os.path.join(root, 'sequences'))
if __name__ == '__main__':
    serve(app, host='0.0.0.0', port=int(os.environ.get('PORT', '18919')), threads=4)
