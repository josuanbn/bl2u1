import os
import zipfile
import shutil
import re
import json
import uuid
import time
import xml.etree.ElementTree as ET
from flask import Flask, render_template, request, send_file, jsonify

app = Flask(__name__)

# CONFIGURATION
UPLOAD_FOLDER = 'uploads'
TEMPLATE_FILE = 'u1_template.3mf'  # Your empty U1 .3mf file
FILAMENT_PROFILES_FILE = 'filament_types.3mf'  # Reference file with available filament profiles
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Ensure folders exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Cleanup settings
MAX_FILE_AGE_HOURS = 8

def cleanup_old_files():
    """Remove files older than MAX_FILE_AGE_HOURS from uploads folder."""
    now = time.time()
    max_age_seconds = MAX_FILE_AGE_HOURS * 3600

    try:
        for filename in os.listdir(UPLOAD_FOLDER):
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            if os.path.isfile(filepath):
                file_age = now - os.path.getmtime(filepath)
                if file_age > max_age_seconds:
                    os.remove(filepath)
    except Exception as e:
        print(f"Cleanup error: {e}")

# Load available filament profiles from filament_types.3mf
AVAILABLE_FILAMENTS = []
try:
    with zipfile.ZipFile(FILAMENT_PROFILES_FILE, 'r') as z:
        settings = json.loads(z.read('Metadata/project_settings.config').decode('utf-8'))
        types = settings.get('filament_type', [])
        ids = settings.get('filament_settings_id', [])
        for t, sid in zip(types, ids):
            AVAILABLE_FILAMENTS.append({
                'type': t,
                'settings_id': sid
            })
    print(f"Loaded {len(AVAILABLE_FILAMENTS)} filament profiles from {FILAMENT_PROFILES_FILE}")
except Exception as e:
    print(f"Warning: Could not load filament profiles: {e}")
    # Fallback defaults
    AVAILABLE_FILAMENTS = [
        {'type': 'PLA', 'settings_id': 'Snapmaker PLA SnapSpeed @U1'},
        {'type': 'PETG', 'settings_id': 'Snapmaker PETG HF'},
        {'type': 'ABS', 'settings_id': 'Generic ABS'},
        {'type': 'TPU', 'settings_id': 'Generic TPU'},
    ]

def normalize_color(color):
    """
    Normalize color to #RRGGBB format for HTML color input compatibility.
    Handles colors with or without #, and with alpha channel (8 chars).
    """
    if not color:
        return "#000000"
    # Remove # if present
    color = color.lstrip('#')
    # If 8 characters (with alpha), take only first 6 (RGB)
    if len(color) == 8:
        color = color[:6]
    # Ensure 6 characters
    if len(color) != 6:
        return "#000000"
    return f"#{color.upper()}"

def parse_bambu_filaments(filepath):
    """
    Opens the 3MF and returns a list of current filaments/colors.
    First tries slice_info.config, then falls back to project_settings.config.
    """
    filaments = []
    try:
        with zipfile.ZipFile(filepath, 'r') as z:
            # First try slice_info.config
            if "Metadata/slice_info.config" in z.namelist():
                with z.open("Metadata/slice_info.config") as f:
                    xml_content = f.read().decode('utf-8')
                    root = ET.fromstring(xml_content)
                    for fil in root.findall(".//filament"):
                        f_id = fil.get('id')
                        f_color = normalize_color(fil.get('color'))
                        f_type = fil.get('type') or 'PLA'
                        filaments.append({
                            'id': f_id,
                            'color': f_color,
                            'type': f_type
                        })

            # If no filaments found in slice_info, try project_settings.config
            if not filaments and "Metadata/project_settings.config" in z.namelist():
                with z.open("Metadata/project_settings.config") as f:
                    settings = json.loads(f.read().decode('utf-8'))
                    colors = settings.get('filament_colour', [])
                    types = settings.get('filament_type', [])

                    for i, color in enumerate(colors):
                        f_type = types[i] if i < len(types) else 'PLA'
                        filaments.append({
                            'id': str(i + 1),  # 1-based IDs
                            'color': normalize_color(color),
                            'type': f_type
                        })

    except Exception as e:
        print(f"Error parsing filaments: {e}")
    return filaments

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/filament-types')
def get_filament_types():
    """Return available filament types for the frontend dropdown."""
    return jsonify(AVAILABLE_FILAMENTS)

@app.route('/analyze', methods=['POST'])
def analyze():
    # Cleanup old files on each upload
    cleanup_old_files()

    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    # Generate unique session ID
    session_id = str(uuid.uuid4())[:8]
    input_filename = f"{session_id}_input.3mf"
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], input_filename)
    file.save(filepath)

    # Analyze colors
    filaments = parse_bambu_filaments(filepath)

    if len(filaments) > 4:
        os.remove(filepath)  # Clean up
        return jsonify({'error': f'Too many colors ({len(filaments)}). The U1 supports a maximum of 4.'}), 400

    return jsonify({
        'session_id': session_id,
        'filaments': filaments
    })

@app.route('/convert', methods=['POST'])
def convert():
    data = request.json
    session_id = data.get('session_id')
    if not session_id:
        return jsonify({'error': 'No session ID provided'}), 400

    input_filename = f"{session_id}_input.3mf"
    output_filename = f"{session_id}_U1_Ready.3mf"
    input_path = os.path.join(app.config['UPLOAD_FOLDER'], input_filename)
    output_path = os.path.join(app.config['UPLOAD_FOLDER'], output_filename)

    if not os.path.exists(input_path):
        return jsonify({'error': 'Session expired or file not found'}), 404

    user_colors = data.get('colors', {})  # Dict {original_filament_id: {color: #hex, type: PLA}}

    # 1. Copy the original file to start
    shutil.copy(input_path, output_path)

    # 2. Read original file's project settings first to determine template
    try:
        with zipfile.ZipFile(input_path, 'r') as z_orig:
            original_project_settings = json.loads(z_orig.read('Metadata/project_settings.config').decode('utf-8'))
    except Exception as e:
        return jsonify({'error': f'Could not read original project settings: {e}'}), 500

    # Determine which template to use based on support settings
    different_settings = original_project_settings.get('different_settings_to_system', [])
    has_support = any('enable_support' in s for s in different_settings if s)
    if has_support:
        template_file = 'u1_template_supports.3mf'
    else:
        template_file = 'u1_template.3mf'

    # 3. Read U1 Template's project settings
    try:
        with zipfile.ZipFile(template_file, 'r') as z_templ:
            u1_project_settings_json = json.loads(z_templ.read('Metadata/project_settings.config').decode('utf-8'))
    except Exception as e:
        return jsonify({'error': f'U1 Template ({template_file}) not found on server: {e}'}), 500

    # 4. Process the 3MF archive
    temp_zip = output_path + ".temp"

    try:
        with zipfile.ZipFile(output_path, 'r') as zin:
            with zipfile.ZipFile(temp_zip, 'w', compression=zipfile.ZIP_DEFLATED) as zout:
                # Get the original slice_info.config to modify it
                slice_info_content = zin.read('Metadata/slice_info.config')

                # --- Start Slice Info Modification ---
                # Change machine model
                xml_str = slice_info_content.decode('utf-8')
                xml_str = re.sub(r'key="printer_model_id" value="[^"]*"', r'key="printer_model_id" value="Snapmaker U1"', xml_str)
                root = ET.fromstring(xml_str)

                # Find the parent of the filament nodes (usually a 'plate' or the root)
                filaments_parent = root.find('.//plate')
                if filaments_parent is None:
                    filaments_parent = root

                all_fil_nodes = filaments_parent.findall('.//filament')

                # Get original filaments in order to map them
                original_filaments = parse_bambu_filaments(input_path)

                # Keep track of which original filaments are being used
                used_original_ids = user_colors.keys()

                # Remove unused filament nodes from the XML
                for fil_node in all_fil_nodes:
                    if fil_node.get('id') not in used_original_ids:
                        filaments_parent.remove(fil_node)

                # Build the ID mapping: old_id -> new_id
                # This is CRITICAL for updating model_settings.config
                id_mapping = {}
                new_id_counter = 1  # U1/Orca uses 1-based IDs for extruders

                # We iterate through the original list to preserve order
                for original_fil in original_filaments:
                    original_id = original_fil['id']
                    if original_id in user_colors:
                        # Find the corresponding node in the XML
                        node_to_update = filaments_parent.find(f".//filament[@id='{original_id}']")
                        if node_to_update is not None:
                            new_conf = user_colors[original_id]

                            # Store the mapping before changing
                            id_mapping[original_id] = str(new_id_counter)

                            # Re-map ID to be sequential (1-based)
                            node_to_update.set('id', str(new_id_counter))

                            # Update color and type
                            node_to_update.set('color', new_conf['color'])
                            node_to_update.set('type', new_conf['type'])

                            new_id_counter += 1

                # Add dummy filaments to reach 4 (white PLA)
                TARGET_FILAMENTS = 4
                while new_id_counter <= TARGET_FILAMENTS:
                    dummy_fil = ET.SubElement(filaments_parent, 'filament')
                    dummy_fil.set('id', str(new_id_counter))
                    dummy_fil.set('type', 'PLA')
                    dummy_fil.set('color', '#FFFFFFFF')
                    dummy_fil.set('used_m', '0')
                    dummy_fil.set('used_g', '0')
                    new_id_counter += 1

                modified_slice_info = ET.tostring(root, encoding='utf-8', xml_declaration=True)
                # --- End Slice Info Modification ---

                # --- Start Model Settings Modification ---
                # We need to update extruder references in model_settings.config
                model_settings_content = zin.read('Metadata/model_settings.config')
                model_root = ET.fromstring(model_settings_content.decode('utf-8'))

                # Find all extruder metadata tags and update them
                for metadata in model_root.findall('.//metadata[@key="extruder"]'):
                    old_extruder = metadata.get('value')
                    if old_extruder in id_mapping:
                        metadata.set('value', id_mapping[old_extruder])

                modified_model_settings = ET.tostring(model_root, encoding='utf-8', xml_declaration=True)
                # --- End Model Settings Modification ---

                # --- Start Project Settings Modification ---
                # Combine U1 printer settings with user-selected filament colors
                # Start with U1 template settings (for printer configuration)
                combined_project_settings = u1_project_settings_json.copy()

                # Get the number of filaments from the original file
                original_filaments = parse_bambu_filaments(input_path)
                num_filaments = len(original_filaments)

                # Build the new filament colors list based on user selections
                # user_colors is keyed by original filament ID
                new_filament_colors = []
                new_filament_types = []

                # Iterate through original filaments in order
                for orig_fil in original_filaments:
                    orig_id = orig_fil['id']
                    if orig_id in user_colors:
                        # User provided color/type for this filament
                        color = user_colors[orig_id]['color']
                        fil_type = user_colors[orig_id]['type']
                    else:
                        # Keep original color/type
                        color = orig_fil['color']
                        fil_type = orig_fil['type']

                    # Ensure color format is correct (with alpha for compatibility)
                    if len(color) == 7:  # #RRGGBB
                        color = color + 'FF'  # Add alpha
                    new_filament_colors.append(color.upper())
                    new_filament_types.append(fil_type)

                # Always ensure 4 filaments (U1 template requirement)
                # Fill missing slots with white PLA
                TARGET_FILAMENTS = 4
                DEFAULT_COLOR = '#FFFFFFFF'
                DEFAULT_TYPE = 'PLA'

                while len(new_filament_colors) < TARGET_FILAMENTS:
                    new_filament_colors.append(DEFAULT_COLOR)
                    new_filament_types.append(DEFAULT_TYPE)

                # Update filament colors in combined settings
                combined_project_settings['filament_colour'] = new_filament_colors

                # Update filament types
                combined_project_settings['filament_type'] = new_filament_types

                # Map filament types to Snapmaker U1 filament profiles
                # Using profiles loaded from filament_types.3mf
                filament_profile_map = {f['type']: f['settings_id'] for f in AVAILABLE_FILAMENTS}
                default_profile = AVAILABLE_FILAMENTS[0]['settings_id'] if AVAILABLE_FILAMENTS else 'Snapmaker PLA SnapSpeed @U1'

                new_filament_settings_ids = []
                for fil_type in new_filament_types:
                    profile = filament_profile_map.get(fil_type, default_profile)
                    new_filament_settings_ids.append(profile)
                combined_project_settings['filament_settings_id'] = new_filament_settings_ids

                # Adjust other filament arrays to always have 4 filaments
                for key in combined_project_settings:
                    if key.startswith('filament_') and isinstance(combined_project_settings[key], list):
                        current_len = len(combined_project_settings[key])
                        if current_len > 0 and current_len != TARGET_FILAMENTS:
                            if TARGET_FILAMENTS > current_len:
                                # Extend by repeating the last value
                                last_val = combined_project_settings[key][-1]
                                combined_project_settings[key].extend([last_val] * (TARGET_FILAMENTS - current_len))
                            else:
                                # Truncate to 4
                                combined_project_settings[key] = combined_project_settings[key][:TARGET_FILAMENTS]

                # Convert to JSON string
                combined_project_settings_str = json.dumps(combined_project_settings, indent=4, ensure_ascii=False)
                # --- End Project Settings Modification ---

                # Write the modified archive
                for item in zin.infolist():
                    # Replace project settings, slice info, and model settings
                    if item.filename == 'Metadata/project_settings.config':
                        zout.writestr(item, combined_project_settings_str.encode('utf-8'))
                    elif item.filename == 'Metadata/slice_info.config':
                        zout.writestr(item, modified_slice_info)
                    elif item.filename == 'Metadata/model_settings.config':
                        zout.writestr(item, modified_model_settings)
                    else:
                        # Copy all other files as-is (including 3D models)
                        content = zin.read(item.filename)
                        zout.writestr(item, content)

        shutil.move(temp_zip, output_path)

        return jsonify({'download_url': f'/download/{output_filename}'})

    except Exception as e:
        if os.path.exists(temp_zip):
            os.remove(temp_zip)
        return jsonify({'error': str(e)}), 500

@app.route('/download/<path:filename>')
def download_file(filename):
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    # Give user a clean filename regardless of internal session ID
    return send_file(filepath, as_attachment=True, download_name='Snapmaker_U1_Ready.3mf')

if __name__ == '__main__':
    app.run(debug=True, port=8080)