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
    echo "  -h, --help           Show this help message"
    exit 1
}

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        -t|--token) GITHUB_TOKEN="$2"; shift ;;
        -d|--dir) INSTALL_DIR="$2"; shift ;;
        -s|--service) SERVICE_NAME="$2"; shift ;;
        -h|--help) usage ;;
        *) echo "Unknown parameter passed: $1"; usage ;;
    esac
    shift
done

echo "Deploying $REPO_NAME to $INSTALL_DIR..."

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

# 7. Restart Service
echo "Restarting service..."
if systemctl list-units --full -all | grep -Fq "$SERVICE_NAME.service"; then
    sudo systemctl restart "$SERVICE_NAME"
    echo "Service $SERVICE_NAME restarted."
else
    echo "Warning: Systemd service '$SERVICE_NAME' not found. Skipping restart."
    echo "You may need to start the bot manually: cd $INSTALL_DIR && source venv/bin/activate && python3 watermarker.py"
fi

echo "Deployment complete!"
