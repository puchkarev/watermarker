#!/bin/bash
set -e

# Configuration
REPO_OWNER="puchkarev"
REPO_NAME="watermarker"
INSTALL_DIR="/opt/watermarker"
SERVICE_NAME="watermarker"
ASSET_NAME="watermarker-dist.zip"

# Function to print usage
usage() {
    echo "Usage: $0 [options]"
    echo "Options:"
    echo "  -t, --token TOKEN    GitHub Personal Access Token (required for private repos)"
    echo "  -d, --dir DIR        Installation directory (default: $INSTALL_DIR)"
    echo "  -s, --service NAME   Systemd service name to restart (default: $SERVICE_NAME)"
    echo "  -b, --bot-token TOKEN Telegram Bot Token (optional, will prompt if missing and config doesn't exist)"
    echo "  -h, --help           Show this help message"
    exit 1
}

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        -t|--token) GITHUB_TOKEN="$2"; shift ;;
        -d|--dir) INSTALL_DIR="$2"; shift ;;
        -s|--service) SERVICE_NAME="$2"; shift ;;
        -b|--bot-token) BOT_TOKEN_ARG="$2"; shift ;;
        -h|--help) usage ;;
        *) echo "Unknown parameter passed: $1"; usage ;;
    esac
    shift
done

echo "Deploying $REPO_NAME to $INSTALL_DIR..."

# 0. Check and Install Dependencies
echo "Checking dependencies..."
MISSING_PKGS=""

# Check for executables
if ! command -v curl &> /dev/null; then MISSING_PKGS="$MISSING_PKGS curl"; fi
if ! command -v unzip &> /dev/null; then MISSING_PKGS="$MISSING_PKGS unzip"; fi
if ! command -v python3 &> /dev/null; then MISSING_PKGS="$MISSING_PKGS python3"; fi

# Check for python3-venv specifically (common issue on Debian/Ubuntu)
if command -v python3 &> /dev/null; then
    if ! python3 -m venv --help &> /dev/null; then
         MISSING_PKGS="$MISSING_PKGS python3-venv"
    fi
fi

if [ -n "$MISSING_PKGS" ]; then
    echo "Missing dependencies detected: $MISSING_PKGS"
    
    INSTALL_CMD=""
    if [ -f /etc/debian_version ]; then
        INSTALL_CMD="apt-get update && apt-get install -y $MISSING_PKGS"
    elif [ -f /etc/redhat-release ]; then
        INSTALL_CMD="yum install -y $MISSING_PKGS"
    fi

    if [ -z "$INSTALL_CMD" ]; then
         echo "Error: Could not detect package manager (apt/yum). Please install: $MISSING_PKGS manually."
         exit 1
    fi

    echo "The script needs to install missing packages using '$INSTALL_CMD'."
    if [ -t 0 ]; then
        read -p "Do you want to switch to root (via sudo) to install them? [y/N] " -n 1 -r
        echo
    else
        # Non-interactive: assume yes if running as root, else fail?
        if [ "$EUID" -eq 0 ]; then
            REPLY="y"
        else
            echo "Non-interactive mode: Cannot ask for permission. Please install dependencies manually."
            exit 1
        fi
    fi

    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "Installing..."
        if [ "$EUID" -ne 0 ]; then
            sudo sh -c "$INSTALL_CMD"
        else
            sh -c "$INSTALL_CMD"
        fi
    else
        echo "Cannot proceed without dependencies. Exiting."
        exit 1
    fi
fi

# 1. Get Latest Release Info
echo "Fetching latest release info..."
if [ -n "$GITHUB_TOKEN" ]; then
    AUTH_HEADER="Authorization: token $GITHUB_TOKEN"
else
    AUTH_HEADER="User-Agent: deploy-script"
fi

LATEST_RELEASE_URL="https://api.github.com/repos/$REPO_OWNER/$REPO_NAME/releases/latest"
RESPONSE=$(curl -s -H "$AUTH_HEADER" "$LATEST_RELEASE_URL")

# Check for errors
if echo "$RESPONSE" | grep -q "Not Found"; then
    echo "Error: Repository or release not found. Check token and repo name."
    exit 1
fi

# 2. Find Asset URL
DOWNLOAD_URL=$(echo "$RESPONSE" | grep -oP '"browser_download_url": "\K(.*watermarker-dist.zip)(?=")')

if [ -z "$DOWNLOAD_URL" ]; then
    echo "Error: Could not find asset '$ASSET_NAME' in the latest release."
    exit 1
fi

echo "Found download URL: $DOWNLOAD_URL"

# 3. Download Asset
TEMP_ZIP="/tmp/watermarker_latest.zip"
echo "Downloading..."
curl -L -H "$AUTH_HEADER" -o "$TEMP_ZIP" "$DOWNLOAD_URL"

# 4. Prepare Install Directory
if [ ! -d "$INSTALL_DIR" ]; then
    echo "Creating installation directory..."
    sudo mkdir -p "$INSTALL_DIR"
    sudo chown $USER:$USER "$INSTALL_DIR"
fi

# 5. Extract
echo "Extracting to $INSTALL_DIR..."
unzip -o "$TEMP_ZIP" -d "$INSTALL_DIR"
rm "$TEMP_ZIP"

# 6. Update Dependencies
echo "Updating dependencies..."
cd "$INSTALL_DIR"
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip install -r requirements.txt

# 7. Setup Configuration
CONFIG_FILE="$INSTALL_DIR/config.json"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "Config file not found."
    TOKEN_TO_USE="$BOT_TOKEN_ARG"
    
    if [ -z "$TOKEN_TO_USE" ]; then
        if [ -t 0 ]; then
             read -p "Enter your Telegram Bot Token: " TOKEN_TO_USE
        else
             echo "Non-interactive mode and no token provided. Skipping config creation."
        fi
    fi
    
    if [ -n "$TOKEN_TO_USE" ]; then
        echo "{ \"bot_token\": \"$TOKEN_TO_USE\" }" > "$CONFIG_FILE"
        echo "Created config.json"
    fi
else
    echo "Config file exists. Skipping setup."
fi

# 8. Setup Systemd Service
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"
if [ ! -f "$SERVICE_FILE" ]; then
    echo "Creating systemd service..."
    # Determine python path
    PYTHON_EXEC="$INSTALL_DIR/venv/bin/python"
    SCRIPT_PATH="$INSTALL_DIR/watermarker.py"
    
    cat <<EOF | sudo tee "$SERVICE_FILE" > /dev/null
[Unit]
Description=Watermarker Telegram Bot
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$PYTHON_EXEC $SCRIPT_PATH
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    
    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME"
    echo "Service $SERVICE_NAME created and enabled."
else
    echo "Service file exists. Skipping setup."
fi

# 9. Restart Service
echo "Restarting service..."
if systemctl list-units --full -all | grep -Fq "$SERVICE_NAME.service"; then
    sudo systemctl restart "$SERVICE_NAME"
    echo "Service $SERVICE_NAME restarted."
else
    echo "Warning: Systemd service '$SERVICE_NAME' not found even after setup attempt."
    echo "You may need to start the bot manually: cd $INSTALL_DIR && source venv/bin/activate && python3 watermarker.py"
fi

echo "Deployment complete!"
