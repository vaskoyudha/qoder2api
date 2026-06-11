#!/bin/bash
set -e

echo "=== qoder2api Installation ==="
echo ""

# Check dependencies
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 is required but not installed"
    exit 1
fi

if ! command -v qodercli &> /dev/null; then
    echo "Warning: qodercli not found in PATH"
    echo "Install qodercli first: https://qoder.com/cli"
    echo ""
    read -p "Continue anyway? (y/N) " -n 1 -r
    echo
    [[ $REPLY =~ ^[Yy]$ ]] || exit 1
fi

# Create installation directory
INSTALL_DIR="$HOME/qoder2api"
mkdir -p "$INSTALL_DIR"

# Copy files
echo "Installing to $INSTALL_DIR..."
cp proxy.py auth_injector.py rotator.py requirements.txt "$INSTALL_DIR/"

# Install Python dependencies
echo "Installing Python dependencies..."
pip3 install --user -r "$INSTALL_DIR/requirements.txt" --quiet

# Check for 9Router database
NINEROUTER_DB="$HOME/.9router/db/data.sqlite"
if [ ! -f "$NINEROUTER_DB" ]; then
    echo ""
    echo "Warning: 9Router database not found at $NINEROUTER_DB"
    echo "qoder2api requires 9Router to manage Qoder accounts."
    echo "Install 9Router: https://github.com/decolua/9router"
fi

# Create systemd service (optional)
echo ""
read -p "Create systemd service for auto-start? (y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    SERVICE_FILE="$HOME/.config/systemd/user/qoder2api.service"
    mkdir -p "$(dirname "$SERVICE_FILE")"
    cat > "$SERVICE_FILE" << EOF
[Unit]
Description=qoder2api - OpenAI-compatible proxy for Qoder CLI
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 $INSTALL_DIR/proxy.py
Restart=on-failure
RestartSec=5
Environment=QODER_PORT=8963
Environment=QODER_TIMEOUT=300
WorkingDirectory=$INSTALL_DIR

[Install]
WantedBy=default.target
EOF
    
    systemctl --user daemon-reload
    systemctl --user enable qoder2api
    systemctl --user start qoder2api
    
    echo "Service created and started!"
    echo "  Check status: systemctl --user status qoder2api"
    echo "  View logs: journalctl --user -u qoder2api -f"
else
    echo ""
    echo "To start manually:"
    echo "  cd $INSTALL_DIR && python3 proxy.py"
fi

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Proxy endpoint: http://127.0.0.1:8963/v1"
echo ""
echo "To use with opencode, add to ~/.config/opencode/opencode.json:"
echo '  "qoder-cli": {'
echo '    "npm": "@ai-sdk/openai-compatible",'
echo '    "name": "Qoder CLI",'
echo '    "options": {'
echo '      "baseURL": "http://127.0.0.1:8963/v1",'
echo '      "apiKey": "not-needed",'
echo '      "timeout": 300000,'
echo '      "chunkTimeout": 120000'
echo '    },'
echo '    "models": {'
echo '      "qoder-unlimited": {'
echo '        "name": "Qoder Qwen3.7-Max Unlimited",'
echo '        "id": "qoder-unlimited",'
echo '        "modalities": { "input": ["text", "image"], "output": ["text"] },'
echo '        "limit": { "context": 128000, "output": 32000 }'
echo '      }'
echo '    }'
echo '  }'
echo ""
echo "Documentation: https://github.com/vaskoyudha/qoder2api"
