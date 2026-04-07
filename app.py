import os
import shutil
import zipfile
import re
import json
import uuid
import time
import logging
import posixpath
import xml.etree.ElementTree as ET
from threading import Timer
from werkzeug.utils import secure_filename as _werkzeug_secure
from flask import Flask, render_template, request, send_file, jsonify
from werkzeug.exceptions import RequestEntityTooLarge
import db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
UPLOAD_FOLDER            = 'uploads'
INVENTORY_FOLDER         = 'inventory'
INVENTORY_DB             = os.path.join(INVENTORY_FOLDER, 'inventory.db')
FILAMENT_PROFILES_FILE   = 'filament_types.3mf'
TARGET_FILAMENTS_MIN     = 4     # pad to at least 4 slots
MAX_FILE_AGE_HOURS       = 8
DEFAULT_FILAMENT_PROFILE = 'Snapmaker PLA SnapSpeed @U1'

app.config['UPLOAD_FOLDER']      = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200 MB

try:
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(INVENTORY_FOLDER, exist_ok=True)
except OSError as e:
    raise RuntimeError(f"Cannot create directories: {e}") from e

db.init_db(INVENTORY_DB)

_SESSION_RE = re.compile(r'^[0-9a-f]{32}$')
_COLOR_RE   = re.compile(r'^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$')

# Maps session_id -> original filename (without .3mf extension)
_session_names: dict[str, str] = {}

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
    # Also purge stale entries from _session_names
    for name in list(_session_names):
        path = _safe_path(f'{name}_input.3mf')
        if path is None or not os.path.exists(path):
            _session_names.pop(name, None)
    t = Timer(interval, _schedule_cleanup, [interval])
    t.daemon = True          # don't prevent process exit
    t.start()


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


def _safe_inventory_path(filename: str) -> str | None:
    safe_dir  = os.path.realpath(INVENTORY_FOLDER)
    candidate = os.path.realpath(os.path.join(safe_dir, filename))
    return candidate if candidate.startswith(safe_dir + os.sep) else None


def is_u1_format(filepath: str) -> bool:
    """Return True if the .3mf already targets Snapmaker U1."""
    try:
        with zipfile.ZipFile(filepath, 'r') as z:
            if 'Metadata/slice_info.config' not in z.namelist():
                return False
            xml_str = z.read('Metadata/slice_info.config').decode('utf-8')
            match = re.search(r'key="printer_model_id"\s+value="([^"]*)"', xml_str)
            return match is not None and 'Snapmaker U1' in match.group(1)
    except Exception:
        return False


def _detect_printer(filepath: str) -> str:
    """Try to detect the source printer model from a .3mf file."""
    try:
        with zipfile.ZipFile(filepath, 'r') as z:
            if 'Metadata/slice_info.config' in z.namelist():
                xml_str = z.read('Metadata/slice_info.config').decode('utf-8')
                match = re.search(r'key="printer_model_id"\s+value="([^"]*)"', xml_str)
                if match:
                    return match.group(1)
    except Exception:
        pass
    return 'Unknown'


def _auto_map_filament_type(original_type: str) -> str:
    """Map an original filament type to the closest available U1 type."""
    if not original_type or not AVAILABLE_FILAMENTS:
        return AVAILABLE_FILAMENTS[0]['type'] if AVAILABLE_FILAMENTS else 'PLA'
    up = original_type.upper()
    for ft in AVAILABLE_FILAMENTS:
        canon = ft['type'].upper().replace('-HF', '').replace('-', '')
        if canon in up:
            return ft['type']
    return AVAILABLE_FILAMENTS[0]['type']


def _auto_convert_for_inventory(input_path: str, output_path: str) -> dict:
    """Auto-convert a Bambu .3mf to U1 and move to inventory.

    Returns metadata dict with filament_count, filament_colors, filament_types.
    Raises on failure.
    """
    filaments = parse_bambu_filaments(input_path)
    if not filaments:
        raise ValueError('Could not parse filaments from the file')

    # Build user_colors with original colors and auto-mapped types
    user_colors = {}
    for fil in filaments:
        user_colors[fil['id']] = {
            'color': fil['color'],
            'type': _auto_map_filament_type(fil['type']),
        }

    # Stage in uploads/ for _do_convert
    temp_sid = uuid.uuid4().hex
    temp_input = _safe_path(f'{temp_sid}_input.3mf')
    if temp_input is None:
        raise RuntimeError('Internal path error')

    shutil.copy2(input_path, temp_input)
    _session_names[temp_sid] = 'inventory_temp'

    try:
        result, status = _do_convert(temp_sid, user_colors)
        if status != 200:
            raise RuntimeError(result.get('error', 'Conversion failed'))

        temp_output = _safe_path(f'{temp_sid}_U1_Ready.3mf')
        if temp_output is None or not os.path.exists(temp_output):
            raise RuntimeError('Converted file not found')

        shutil.move(temp_output, output_path)
    finally:
        # Clean up temp files
        _session_names.pop(temp_sid, None)
        for suffix in ('_input.3mf', '_U1_Ready.3mf'):
            p = _safe_path(f'{temp_sid}{suffix}')
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    colors = [user_colors[f['id']]['color'] for f in filaments]
    types  = [user_colors[f['id']]['type'] for f in filaments]
    return {
        'filament_count': len(filaments),
        'filament_colors': [normalize_color(c) for c in colors],
        'filament_types': types,
    }


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

    # Store the original name (sanitised, without extension) for the download
    raw_name = file.filename.rsplit('.', 1)[0] if '.' in file.filename else file.filename
    safe_name = _werkzeug_secure(raw_name) or 'converted'

    session_id     = uuid.uuid4().hex          # 32 hex chars, full 128-bit entropy
    _session_names[session_id] = safe_name
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


# ---------------------------------------------------------------------------
# Core conversion logic (used by /convert and /convert-batch)
# ---------------------------------------------------------------------------
def _do_convert(session_id: str, user_colors: dict) -> tuple[dict, int]:
    """Convert a single session's .3mf file. Returns (result_dict, http_status)."""
    if not _SESSION_RE.fullmatch(session_id):
        return {'error': 'Invalid session ID'}, 400

    input_path  = _safe_path(f'{session_id}_input.3mf')
    output_path = _safe_path(f'{session_id}_U1_Ready.3mf')
    if input_path is None or output_path is None:
        return {'error': 'Internal path error'}, 500

    if not os.path.exists(input_path):
        return {'error': 'Session expired or file not found. Please re-upload.'}, 404

    if not isinstance(user_colors, dict):
        return {'error': '"colors" must be a JSON object'}, 400

    original_filaments = parse_bambu_filaments(input_path)
    if not original_filaments:
        return {'error': 'Could not parse filaments from the uploaded file'}, 400

    valid_ids = {f['id'] for f in original_filaments}

    for fid, conf in user_colors.items():
        if fid not in valid_ids:
            return {'error': f'Unknown filament ID: {fid}'}, 400
        if not isinstance(conf, dict):
            return {'error': 'Each filament entry must be a JSON object'}, 400
        color = conf.get('color', '')
        ftype = conf.get('type', '')
        if not _COLOR_RE.match(color):
            return {'error': f'Invalid color: {color}'}, 400
        if ftype not in _VALID_TYPES:
            return {'error': f'Invalid filament type: {ftype}'}, 400

    try:
        with zipfile.ZipFile(input_path, 'r') as z:
            orig_settings = json.loads(z.read('Metadata/project_settings.config').decode('utf-8'))
    except Exception as e:
        logger.error("Could not read project settings [%s]: %s", session_id, e)
        return {'error': 'Could not read project settings from the uploaded file'}, 500

    diff        = orig_settings.get('different_settings_to_system', [])
    has_support = any(isinstance(s, str) and 'enable_support' in s for s in diff)
    template    = 'u1_template_supports.3mf' if has_support else 'u1_template.3mf'

    try:
        with zipfile.ZipFile(template, 'r') as z:
            u1_settings = json.loads(z.read('Metadata/project_settings.config').decode('utf-8'))
    except Exception as e:
        logger.error("Could not read template %s: %s", template, e)
        return {'error': 'Server template missing -- please contact the administrator'}, 500

    try:
        with zipfile.ZipFile(input_path, 'r') as zin, \
             zipfile.ZipFile(output_path, 'w', compression=zipfile.ZIP_DEFLATED) as zout:

            xml_str = zin.read('Metadata/slice_info.config').decode('utf-8')
            xml_str = re.sub(
                r'key="printer_model_id" value="[^"]*"',
                'key="printer_model_id" value="Snapmaker U1"',
                xml_str,
            )
            root = ET.fromstring(xml_str)

            filaments_parent = root.find('.//plate') or root
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

            target_filaments = max(TARGET_FILAMENTS_MIN, len(user_colors))

            while new_id_counter <= target_filaments:
                dummy = ET.SubElement(filaments_parent, 'filament')
                dummy.set('id',     str(new_id_counter))
                dummy.set('type',   'PLA')
                dummy.set('color',  '#FFFFFFFF')
                dummy.set('used_m', '0')
                dummy.set('used_g', '0')
                new_id_counter += 1

            modified_slice_info = ET.tostring(root, encoding='utf-8', xml_declaration=True)

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

            combined   = u1_settings.copy()
            new_colors: list[str] = []
            new_types:  list[str] = []

            for fil in original_filaments:
                fid = fil['id']
                if fid not in user_colors:
                    continue
                color = user_colors[fid]['color']
                ftype = user_colors[fid]['type']
                color = (color + 'FF') if len(color) == 7 else color
                new_colors.append(color.upper())
                new_types.append(ftype)

            while len(new_colors) < target_filaments:
                new_colors.append('#FFFFFFFF')
                new_types.append('PLA')

            combined['filament_colour'] = new_colors
            combined['filament_type']   = new_types

            profile_map     = {f['type']: f['settings_id'] for f in AVAILABLE_FILAMENTS}
            default_profile = AVAILABLE_FILAMENTS[0]['settings_id'] if AVAILABLE_FILAMENTS else DEFAULT_FILAMENT_PROFILE
            combined['filament_settings_id'] = [
                profile_map.get(t, default_profile) for t in new_types
            ]

            for key, val in combined.items():
                if key.startswith('filament_') and isinstance(val, list) and 0 < len(val) != target_filaments:
                    if len(val) < target_filaments:
                        val.extend([val[-1]] * (target_filaments - len(val)))
                    else:
                        combined[key] = val[:target_filaments]

            combined_bytes = json.dumps(combined, indent=4, ensure_ascii=False).encode('utf-8')

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

        return {
            'download_url': f'/download/{session_id}_U1_Ready.3mf',
            'download_name': f'{_session_names.get(session_id, "converted")}-U1.3mf',
        }, 200

    except Exception as e:
        logger.error("Conversion error [%s]: %s", session_id, e, exc_info=True)
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
        return {'error': 'Conversion failed. Please check your file and try again.'}, 500


@app.route('/convert', methods=['POST'])
def convert():
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({'error': 'Invalid JSON body'}), 400

    session_id  = data.get('session_id', '')
    user_colors = data.get('colors', {})
    result, status = _do_convert(session_id, user_colors)
    return jsonify(result), status


@app.route('/convert-batch', methods=['POST'])
def convert_batch():
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({'error': 'Invalid JSON body'}), 400

    sessions = data.get('sessions', {})
    if not isinstance(sessions, dict) or not sessions:
        return jsonify({'error': '"sessions" must be a non-empty object'}), 400

    results = []
    errors  = []
    for sid, conf in sessions.items():
        colors = conf.get('colors', {}) if isinstance(conf, dict) else {}
        result, status = _do_convert(sid, colors)
        if status == 200:
            result['session_id'] = sid
            results.append(result)
        else:
            result['session_id'] = sid
            errors.append(result)

    return jsonify({'results': results, 'errors': errors}), 200 if results else 500


@app.route('/download-zip', methods=['POST'])
def download_zip():
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({'error': 'Invalid JSON body'}), 400

    session_ids = data.get('session_ids', [])
    if not isinstance(session_ids, list) or not session_ids:
        return jsonify({'error': '"session_ids" must be a non-empty list'}), 400

    # Validate all session IDs first
    for sid in session_ids:
        if not _SESSION_RE.fullmatch(sid):
            return jsonify({'error': f'Invalid session ID: {sid}'}), 400

    bundle_name = f'{uuid.uuid4().hex}_bundle.zip'
    bundle_path = _safe_path(bundle_name)
    if bundle_path is None:
        return jsonify({'error': 'Internal path error'}), 500

    used_names: dict[str, int] = {}
    try:
        with zipfile.ZipFile(bundle_path, 'w', compression=zipfile.ZIP_DEFLATED) as zout:
            for sid in session_ids:
                src = _safe_path(f'{sid}_U1_Ready.3mf')
                if src is None or not os.path.exists(src):
                    continue
                friendly = f'{_session_names.get(sid, "converted")}-U1.3mf'
                # Deduplicate names
                if friendly in used_names:
                    used_names[friendly] += 1
                    base, ext = friendly.rsplit('.', 1)
                    friendly = f'{base} ({used_names[friendly]}).{ext}'
                else:
                    used_names[friendly] = 1
                zout.write(src, friendly)

        if not os.path.getsize(bundle_path):
            os.remove(bundle_path)
            return jsonify({'error': 'No converted files found'}), 404

        return send_file(bundle_path, as_attachment=True, download_name='converted_files.zip')
    except Exception as e:
        logger.error("Bundle ZIP error: %s", e, exc_info=True)
        if os.path.exists(bundle_path):
            try:
                os.remove(bundle_path)
            except OSError:
                pass
        return jsonify({'error': 'Failed to create ZIP bundle'}), 500


@app.route('/download/<filename>')
def download_file(filename: str):
    # Strict allowlist: only session-prefixed output files
    if not re.fullmatch(r'[0-9a-f]{32}_U1_Ready\.3mf', filename):
        return jsonify({'error': 'Invalid filename'}), 400
    filepath = _safe_path(filename)
    if filepath is None or not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404
    # Use original filename if available
    session_id = filename[:32]
    download_name = f'{_session_names.get(session_id, "converted")}-U1.3mf'
    return send_file(filepath, as_attachment=True, download_name=download_name)


# ---------------------------------------------------------------------------
# Inventory API
# ---------------------------------------------------------------------------
@app.route('/api/inventory')
def inventory_list():
    sort_by = request.args.get('sort', 'upload_date')
    order   = request.args.get('order', 'desc')
    search  = request.args.get('q', '')
    items   = db.list_items(sort_by=sort_by, order=order, search=search)
    return jsonify(items)


@app.route('/api/inventory/upload', methods=['POST'])
def inventory_upload():
    files = request.files.getlist('files')
    if not files:
        return jsonify({'error': 'No files uploaded'}), 400

    items  = []
    errors = []

    for file in files:
        if not file.filename:
            continue
        if not file.filename.lower().endswith('.3mf'):
            errors.append({'filename': file.filename, 'error': 'Not a .3mf file'})
            continue

        raw_name  = file.filename.rsplit('.', 1)[0] if '.' in file.filename else file.filename
        safe_name = _werkzeug_secure(raw_name) or 'model'
        original_name = f'{safe_name}.3mf'

        # Save to temp location first
        item_id   = uuid.uuid4().hex
        temp_path = _safe_path(f'{item_id}_inv_temp.3mf')
        if temp_path is None:
            errors.append({'filename': file.filename, 'error': 'Internal path error'})
            continue

        try:
            file.save(temp_path)

            # Validate ZIP magic bytes
            with open(temp_path, 'rb') as f:
                magic = f.read(4)
            if magic != b'PK\x03\x04':
                raise ValueError('Not a valid 3MF/ZIP archive')

            stored_name = f'{item_id}.3mf'
            dest_path   = _safe_inventory_path(stored_name)
            if dest_path is None:
                raise RuntimeError('Internal path error')

            source_printer = _detect_printer(temp_path)
            already_u1     = is_u1_format(temp_path)

            if already_u1:
                # Already U1 — just move to inventory
                filaments = parse_bambu_filaments(temp_path)
                shutil.move(temp_path, dest_path)
                meta = {
                    'filament_count': len(filaments),
                    'filament_colors': [normalize_color(f['color']) for f in filaments],
                    'filament_types': [f.get('type', 'PLA') for f in filaments],
                }
                was_converted = False
            else:
                # Auto-convert to U1
                meta = _auto_convert_for_inventory(temp_path, dest_path)
                was_converted = True

            file_size = os.path.getsize(dest_path)
            item = db.add_item(
                item_id=item_id,
                original_name=original_name,
                stored_name=stored_name,
                file_size=file_size,
                filament_count=meta['filament_count'],
                filament_colors=meta['filament_colors'],
                filament_types=meta['filament_types'],
                was_converted=was_converted,
                source_printer=source_printer,
                title=safe_name,
            )
            items.append(item)

        except Exception as e:
            logger.error("Inventory upload error for %s: %s", file.filename, e, exc_info=True)
            errors.append({'filename': file.filename, 'error': str(e)})
        finally:
            # Clean up temp file if it still exists
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    return jsonify({'items': items, 'errors': errors}), 200 if items else 400


@app.route('/api/inventory/<item_id>/download')
def inventory_download(item_id: str):
    if not _SESSION_RE.fullmatch(item_id):
        return jsonify({'error': 'Invalid ID'}), 400

    item = db.get_item(item_id)
    if item is None:
        return jsonify({'error': 'Item not found'}), 404

    filepath = _safe_inventory_path(item['stored_name'])
    if filepath is None or not os.path.exists(filepath):
        return jsonify({'error': 'File not found on disk'}), 404

    return send_file(filepath, as_attachment=True, download_name=item['original_name'])


@app.route('/api/inventory/<item_id>', methods=['PATCH'])
def inventory_update(item_id: str):
    if not _SESSION_RE.fullmatch(item_id):
        return jsonify({'error': 'Invalid ID'}), 400

    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({'error': 'Invalid JSON body'}), 400

    item = db.get_item(item_id)
    if item is None:
        return jsonify({'error': 'Item not found'}), 404

    kwargs = {}
    if 'title' in data:
        kwargs['title'] = str(data['title'])[:200]
    if 'description' in data:
        kwargs['description'] = str(data['description'])[:2000]
    if 'tags' in data:
        tags = data['tags']
        if isinstance(tags, list):
            kwargs['tags'] = [str(t).strip()[:50] for t in tags if str(t).strip()][:20]
        elif isinstance(tags, str):
            kwargs['tags'] = [t.strip()[:50] for t in tags.split(',') if t.strip()][:20]

    updated = db.update_item(item_id, **kwargs)
    return jsonify(updated)


@app.route('/api/inventory/<item_id>', methods=['DELETE'])
def inventory_delete(item_id: str):
    if not _SESSION_RE.fullmatch(item_id):
        return jsonify({'error': 'Invalid ID'}), 400

    item = db.get_item(item_id)
    if item is None:
        return jsonify({'error': 'Item not found'}), 404

    # Remove file from disk
    filepath = _safe_inventory_path(item['stored_name'])
    if filepath and os.path.exists(filepath):
        try:
            os.remove(filepath)
        except OSError as e:
            logger.error("Could not delete inventory file %s: %s", filepath, e)

    db.delete_item(item_id)
    return jsonify({'ok': True})


if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(debug=debug, port=8080)
