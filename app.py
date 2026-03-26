import os
import zipfile
import re
import json
import uuid
import time
import logging
import posixpath
import xml.etree.ElementTree as ET
from threading import Timer
from flask import Flask, render_template, request, send_file, jsonify
from werkzeug.exceptions import RequestEntityTooLarge

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
UPLOAD_FOLDER            = 'uploads'
FILAMENT_PROFILES_FILE   = 'filament_types.3mf'
TARGET_FILAMENTS         = 4     # U1 hardware supports 4 extruders
MAX_FILE_AGE_HOURS       = 8
DEFAULT_FILAMENT_PROFILE = 'Snapmaker PLA SnapSpeed @U1'

app.config['UPLOAD_FOLDER']      = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200 MB

try:
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
except OSError as e:
    raise RuntimeError(f"Cannot create upload directory: {e}") from e

_SESSION_RE = re.compile(r'^[0-9a-f]{32}$')
_COLOR_RE   = re.compile(r'^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$')

# Maps session_id -> original filename stem (for download naming)
_session_names: dict[str, str] = {}


def _sanitize_download_name(name: str) -> str:
    """Remove characters invalid in filenames while preserving non-ASCII."""
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '', name).strip()
    return cleaned or 'converted'

# ---------------------------------------------------------------------------
# Filament profiles (loaded once at startup)
# ---------------------------------------------------------------------------
AVAILABLE_FILAMENTS: list[dict] = []
try:
    with zipfile.ZipFile(FILAMENT_PROFILES_FILE, 'r') as z:
        settings = json.loads(z.read('Metadata/project_settings.config').decode('utf-8'))
        for t, sid in zip(settings.get('filament_type', []), settings.get('filament_settings_id', [])):
            AVAILABLE_FILAMENTS.append({'type': t, 'settings_id': sid})
    logger.info("Loaded %d filament profiles", len(AVAILABLE_FILAMENTS))
except Exception as e:
    logger.warning("Could not load filament profiles (%s) -- using fallback defaults", e)
    AVAILABLE_FILAMENTS = [
        {'type': 'PLA',  'settings_id': DEFAULT_FILAMENT_PROFILE},
        {'type': 'PETG', 'settings_id': 'Snapmaker PETG HF'},
        {'type': 'ABS',  'settings_id': 'Generic ABS'},
        {'type': 'TPU',  'settings_id': 'Generic TPU'},
    ]

_VALID_TYPES = {f['type'] for f in AVAILABLE_FILAMENTS}

# ---------------------------------------------------------------------------
# Background cleanup (every hour instead of on every request)
# ---------------------------------------------------------------------------
def cleanup_old_files() -> None:
    now    = time.time()
    cutoff = MAX_FILE_AGE_HOURS * 3600
    try:
        for name in os.listdir(UPLOAD_FOLDER):
            path = os.path.join(UPLOAD_FOLDER, name)
            if os.path.isfile(path) and (now - os.path.getmtime(path)) > cutoff:
                os.remove(path)
                logger.debug("Deleted old upload: %s", name)
    except Exception as e:
        logger.error("Cleanup error: %s", e)


def _schedule_cleanup(interval: int = 3600) -> None:
    cleanup_old_files()
    for sid in list(_session_names):
        if _safe_path(f'{sid}_input.3mf') is None or not os.path.exists(_safe_path(f'{sid}_input.3mf')):
            _session_names.pop(sid, None)
    Timer(interval, _schedule_cleanup, [interval]).start()


_schedule_cleanup()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def normalize_color(color: str) -> str:
    if not color:
        return '#000000'
    c = color.lstrip('#')
    if len(c) == 8:
        c = c[:6]
    if len(c) != 6:
        return '#000000'
    try:
        int(c, 16)
    except ValueError:
        return '#000000'
    return f'#{c.upper()}'


def parse_bambu_filaments(filepath: str) -> list[dict]:
    filaments: list[dict] = []
    try:
        with zipfile.ZipFile(filepath, 'r') as z:
            names = z.namelist()
            if 'Metadata/slice_info.config' in names:
                root = ET.fromstring(z.read('Metadata/slice_info.config').decode('utf-8'))
                for fil in root.findall('.//filament'):
                    filaments.append({
                        'id':    fil.get('id'),
                        'color': normalize_color(fil.get('color', '')),
                        'type':  fil.get('type') or 'PLA',
                    })
            if not filaments and 'Metadata/project_settings.config' in names:
                cfg    = json.loads(z.read('Metadata/project_settings.config').decode('utf-8'))
                colors = cfg.get('filament_colour', [])
                types  = cfg.get('filament_type', [])
                for i, color in enumerate(colors):
                    filaments.append({
                        'id':    str(i + 1),
                        'color': normalize_color(color),
                        'type':  types[i] if i < len(types) else 'PLA',
                    })
    except Exception as e:
        logger.error("Error parsing filaments from %s: %s", filepath, e)
    return filaments


def _safe_path(filename: str) -> str | None:
    safe_dir  = os.path.realpath(UPLOAD_FOLDER)
    candidate = os.path.realpath(os.path.join(safe_dir, filename))
    return candidate if candidate.startswith(safe_dir + os.sep) else None

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(_e):
    return jsonify({'error': 'File is too large. Maximum size is 200 MB.'}), 413


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/filament-types')
def get_filament_types():
    return jsonify(AVAILABLE_FILAMENTS)


@app.route('/analyze', methods=['POST'])
def analyze():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No file selected'}), 400
    if not file.filename.lower().endswith('.3mf'):
        return jsonify({'error': 'Only .3mf files are accepted'}), 400

    session_id     = uuid.uuid4().hex          # 32 hex chars, full 128-bit entropy
    raw_name = file.filename.rsplit('.', 1)[0] if '.' in file.filename else file.filename
    _session_names[session_id] = _sanitize_download_name(raw_name)
    input_filename = f'{session_id}_input.3mf'
    filepath       = _safe_path(input_filename)
    if filepath is None:
        return jsonify({'error': 'Internal path error'}), 500

    file.save(filepath)

    # Validate ZIP magic bytes
    with open(filepath, 'rb') as f:
        magic = f.read(4)
    if magic != b'PK\x03\x04':
        os.remove(filepath)
        return jsonify({'error': 'Uploaded file is not a valid 3MF/ZIP archive'}), 400

    filaments = parse_bambu_filaments(filepath)
    return jsonify({'session_id': session_id, 'filaments': filaments})


@app.route('/convert', methods=['POST'])
def convert():
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({'error': 'Invalid JSON body'}), 400

    session_id = data.get('session_id', '')
    if not _SESSION_RE.fullmatch(session_id):
        return jsonify({'error': 'Invalid session ID'}), 400

    input_path  = _safe_path(f'{session_id}_input.3mf')
    output_path = _safe_path(f'{session_id}_U1_Ready.3mf')
    if input_path is None or output_path is None:
        return jsonify({'error': 'Internal path error'}), 500

    if not os.path.exists(input_path):
        return jsonify({'error': 'Session expired or file not found. Please re-upload.'}), 404

    user_colors = data.get('colors', {})
    if not isinstance(user_colors, dict):
        return jsonify({'error': '"colors" must be a JSON object'}), 400

    # Parse filaments once and reuse throughout
    original_filaments = parse_bambu_filaments(input_path)
    if not original_filaments:
        return jsonify({'error': 'Could not parse filaments from the uploaded file'}), 400

    valid_ids = {f['id'] for f in original_filaments}

    for fid, conf in user_colors.items():
        if fid not in valid_ids:
            return jsonify({'error': f'Unknown filament ID: {fid}'}), 400
        if not isinstance(conf, dict):
            return jsonify({'error': 'Each filament entry must be a JSON object'}), 400
        color = conf.get('color', '')
        ftype = conf.get('type', '')
        if not _COLOR_RE.match(color):
            return jsonify({'error': f'Invalid color: {color}'}), 400
        if ftype not in _VALID_TYPES:
            return jsonify({'error': f'Invalid filament type: {ftype}'}), 400

    # Pick template based on support settings in the original file
    try:
        with zipfile.ZipFile(input_path, 'r') as z:
            orig_settings = json.loads(z.read('Metadata/project_settings.config').decode('utf-8'))
    except Exception as e:
        logger.error("Could not read project settings [%s]: %s", session_id, e)
        return jsonify({'error': 'Could not read project settings from the uploaded file'}), 500

    diff        = orig_settings.get('different_settings_to_system', [])
    has_support = any(isinstance(s, str) and 'enable_support' in s for s in diff)
    template    = 'u1_template_supports.3mf' if has_support else 'u1_template.3mf'

    try:
        with zipfile.ZipFile(template, 'r') as z:
            u1_settings = json.loads(z.read('Metadata/project_settings.config').decode('utf-8'))
    except Exception as e:
        logger.error("Could not read template %s: %s", template, e)
        return jsonify({'error': 'Server template missing -- please contact the administrator'}), 500

    # ------------------------------------------------------------------
    # Build the modified archive: read input_path -> write output_path
    # (no intermediate shutil.copy needed)
    # ------------------------------------------------------------------
    try:
        with zipfile.ZipFile(input_path, 'r') as zin, \
             zipfile.ZipFile(output_path, 'w', compression=zipfile.ZIP_DEFLATED) as zout:

            # ---- slice_info.config ----------------------------------------
            xml_str = zin.read('Metadata/slice_info.config').decode('utf-8')
            xml_str = re.sub(
                r'key="printer_model_id" value="[^"]*"',
                'key="printer_model_id" value="Snapmaker U1"',
                xml_str,
            )
            root = ET.fromstring(xml_str)

            filaments_parent = root.find('.//plate') or root

            # Use direct children only so Element.remove() targets the right parent
            existing_nodes = filaments_parent.findall('filament')

            id_mapping: dict[str, str] = {}
            new_id_counter = 1

            for node in list(existing_nodes):
                old_id = node.get('id')
                if old_id not in user_colors:
                    filaments_parent.remove(node)
                else:
                    conf = user_colors[old_id]
                    id_mapping[old_id] = str(new_id_counter)
                    node.set('id',    str(new_id_counter))
                    node.set('color', conf['color'])
                    node.set('type',  conf['type'])
                    new_id_counter += 1

            # Pad to TARGET_FILAMENTS with dummy white-PLA entries
            while new_id_counter <= TARGET_FILAMENTS:
                dummy = ET.SubElement(filaments_parent, 'filament')
                dummy.set('id',     str(new_id_counter))
                dummy.set('type',   'PLA')
                dummy.set('color',  '#FFFFFFFF')
                dummy.set('used_m', '0')
                dummy.set('used_g', '0')
                new_id_counter += 1

            modified_slice_info = ET.tostring(root, encoding='utf-8', xml_declaration=True)

            # ---- model_settings.config ------------------------------------
            model_root = ET.fromstring(
                zin.read('Metadata/model_settings.config').decode('utf-8')
            )
            for meta in model_root.findall('.//metadata[@key="extruder"]'):
                old_ext = meta.get('value')
                if old_ext in id_mapping:
                    meta.set('value', id_mapping[old_ext])

            modified_model_settings = ET.tostring(
                model_root, encoding='utf-8', xml_declaration=True
            )

            # ---- project_settings.config ----------------------------------
            combined   = u1_settings.copy()
            new_colors: list[str] = []
            new_types:  list[str] = []

            for fil in original_filaments:
                fid = fil['id']
                if fid not in user_colors:
                    continue
                color = user_colors[fid]['color']
                ftype = user_colors[fid]['type']
                color = (color + 'FF') if len(color) == 7 else color  # ensure RGBA
                new_colors.append(color.upper())
                new_types.append(ftype)

            while len(new_colors) < TARGET_FILAMENTS:
                new_colors.append('#FFFFFFFF')
                new_types.append('PLA')

            combined['filament_colour'] = new_colors
            combined['filament_type']   = new_types

            profile_map     = {f['type']: f['settings_id'] for f in AVAILABLE_FILAMENTS}
            default_profile = AVAILABLE_FILAMENTS[0]['settings_id'] if AVAILABLE_FILAMENTS else DEFAULT_FILAMENT_PROFILE
            combined['filament_settings_id'] = [
                profile_map.get(t, default_profile) for t in new_types
            ]

            # Normalise all filament_* arrays to TARGET_FILAMENTS length
            for key, val in combined.items():
                if key.startswith('filament_') and isinstance(val, list) and 0 < len(val) != TARGET_FILAMENTS:
                    if len(val) < TARGET_FILAMENTS:
                        val.extend([val[-1]] * (TARGET_FILAMENTS - len(val)))
                    else:
                        combined[key] = val[:TARGET_FILAMENTS]

            combined_bytes = json.dumps(combined, indent=4, ensure_ascii=False).encode('utf-8')

            # ---- Copy all members, sanitising paths (Zip Slip defence) ----
            for item in zin.infolist():
                safe_name = posixpath.normpath(item.filename).lstrip('/')
                if safe_name.startswith('..'):
                    logger.warning("Skipping suspicious ZIP entry: %s", item.filename)
                    continue

                if item.filename == 'Metadata/project_settings.config':
                    zout.writestr(item, combined_bytes)
                elif item.filename == 'Metadata/slice_info.config':
                    zout.writestr(item, modified_slice_info)
                elif item.filename == 'Metadata/model_settings.config':
                    zout.writestr(item, modified_model_settings)
                else:
                    zout.writestr(item, zin.read(item.filename))

        return jsonify({'download_url': f'/download/{session_id}_U1_Ready.3mf'})

    except Exception as e:
        logger.error("Conversion error [%s]: %s", session_id, e, exc_info=True)
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
        return jsonify({'error': 'Conversion failed. Please check your file and try again.'}), 500


@app.route('/download/<filename>')
def download_file(filename: str):
    # Strict allowlist: only session-prefixed output files
    if not re.fullmatch(r'[0-9a-f]{32}_U1_Ready\.3mf', filename):
        return jsonify({'error': 'Invalid filename'}), 400
    filepath = _safe_path(filename)
    if filepath is None or not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404
    session_id    = filename[:32]
    original_name = _session_names.get(session_id, 'converted')
    return send_file(filepath, as_attachment=True, download_name=f'{original_name}-U1.3mf')


if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(debug=debug, port=8080)
