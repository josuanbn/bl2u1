# Bambu Lab to Snapmaker U1 Converter

A web-based tool to convert Bambu Lab .3mf projects to Snapmaker U1 format, preserving multi-color painting and filament assignments.

**Live version:** [https://bl2u1.nbn.cat](https://bl2u1.nbn.cat)

## Features

- Converts Bambu Lab/Bambu Studio .3mf files to Snapmaker U1 compatible format
- Preserves color painting and multi-color assignments
- Applies the 0.20mm Standard print profile for U1
- Remaps filament types to U1 compatible profiles
- Automatically enables Tree Supports (auto) if the original model has supports enabled
- Supports up to 4 filaments/colors
- Simple drag & drop interface
- No installation required (web-based)

## How It Works

1. Upload your Bambu Lab .3mf file
2. Review and adjust filament colors/types if needed
3. Click "Convert and Download"
4. Open the converted file in **Snapmaker Orca** for final slicing

## Self-Hosting

### Requirements

- Python 3.8+
- Flask

### Installation

```bash
# Clone the repository
git clone https://github.com/josua/bl2u1.git
cd bl2u1

# Install dependencies
pip install flask

# Run the application
python app.py
```

The application will be available at `http://localhost:8080`

### Project Structure

```
bambu-to-u1-web/
├── app.py                    # Flask backend
├── templates/
│   └── index.html            # Frontend interface
├── uploads/                  # Temporary file storage (auto-cleaned)
├── u1_template.3mf           # U1 template without supports
├── u1_template_supports.3mf  # U1 template with tree supports
└── filament_types.3mf        # Available filament profiles
```

### Template Files

The converter requires template .3mf files configured for Snapmaker U1:

- `u1_template.3mf` - Base template with 0.20mm Standard profile, supports disabled
- `u1_template_supports.3mf` - Same as above but with Tree Supports (auto) enabled
- `filament_types.3mf` - Reference file containing available U1 filament profiles

## Technical Details

The converter performs the following transformations:

1. **Printer Profile**: Changes printer settings from Bambu Lab to Snapmaker U1
2. **Filament Mapping**: Remaps filament types to U1 compatible profiles
3. **Color Preservation**: Maintains all color painting data from the original file
4. **Support Detection**: Checks `different_settings_to_system` for `enable_support` and uses the appropriate template
5. **Filament Padding**: Ensures 4 filaments are always configured (fills empty slots with white PLA)

### File Cleanup

Uploaded files are automatically deleted after 8 hours to save disk space.

## Limitations

- Maximum 4 filaments/colors (U1 hardware limitation)
- The converted file must be sliced in Snapmaker Orca before printing
- Some advanced Bambu-specific features may not transfer

## Contributing

Contributions are welcome! Feel free to:

- Report bugs
- Suggest features
- Submit pull requests

## License

MIT License - feel free to use, modify, and distribute.

## Acknowledgments

- Snapmaker community for feedback and testing
- Bambu Lab for the excellent .3mf format documentation

## Support

If you find this tool useful, consider [buying me a coffee](https://buymeacoffee.com/josuanbn)!
