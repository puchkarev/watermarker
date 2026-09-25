#!/bin/bash
# Deploys the watermarker bot to AWS Lambda (on demand, webhook-driven).
# See serverless/README.md for the full walkthrough.
#
# Usage: ./serverless/deploy.sh [command] [options]
#
# Commands:
#   deploy   (default) build the package, create/update the AWS stack, point the bot's webhook at it
#   attach   point the bot's webhook at the Lambda again (e.g. after "detach")
#   detach   remove the webhook so a polling copy of watermarker.py can use the same bot token
#   status   show the stack outputs and Telegram's view of the webhook
#   logs     follow the function's logs
#   remove   remove the webhook and delete the stack (the settings bucket is kept)
#
# Options:
#   --bot-token TOKEN        Telegram bot token (asked for on first deploy; kept on later deploys)
#   --allowed-chat-ids IDS   comma-separated chat ids allowed to use the bot ("" allows everyone)
#   --memory MB              Lambda memory, 512-10240 (default 2048)
#   --stack-name NAME        CloudFormation stack and function name (default watermarker-bot)
#   --region REGION          AWS region (default: your AWS CLI default region)
#   -h, --help               show this help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$SCRIPT_DIR/build"
PACKAGE="$BUILD_DIR/watermarker-lambda.zip"

COMMAND="deploy"
STACK_NAME="watermarker-bot"
BOT_TOKEN_ARG=""
ALLOWED_CHAT_IDS=""
ALLOWED_SET="false"
MEMORY=""

usage() { sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; }
info() { echo "==> $*"; }
fail() { echo "Error: $*" >&2; exit 1; }

# --- Parse arguments ---

while [[ $# -gt 0 ]]; do
    case "$1" in
        deploy|attach|detach|status|logs|remove) COMMAND="$1"; shift ;;
        --bot-token) BOT_TOKEN_ARG="${2:?--bot-token needs a value}"; shift 2 ;;
        --allowed-chat-ids) ALLOWED_CHAT_IDS="${2-}"; ALLOWED_SET="true"; shift 2 ;;
        --memory) MEMORY="${2:?--memory needs a value}"; shift 2 ;;
        --stack-name) STACK_NAME="${2:?--stack-name needs a value}"; shift 2 ;;
        --region) export AWS_REGION="${2:?--region needs a value}"; export AWS_DEFAULT_REGION="$AWS_REGION"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
    esac
done

# --- Checks ---

check_prerequisites() {
    local missing=""
    for tool in aws python3 curl zip; do
        command -v "$tool" &> /dev/null || missing="$missing $tool"
    done
    [[ -z "$missing" ]] || fail "missing required tools:$missing (see serverless/README.md, step 1)"
    python3 -m pip --version &> /dev/null || fail "pip is not available for python3 (install python3-pip)"

    aws sts get-caller-identity --query Account --output text > /dev/null 2>&1 \
        || fail "the AWS CLI has no working credentials. Run 'aws configure' (see serverless/README.md, step 2)"
    if [[ -z "${AWS_REGION:-}" && -z "$(aws configure get region 2>/dev/null || true)" ]]; then
        fail "no AWS region set. Pass --region (e.g. --region us-east-1) or run 'aws configure'"
    fi
}

stack_exists() {
    aws cloudformation describe-stacks --stack-name "$STACK_NAME" > /dev/null 2>&1
}

stack_output() {
    aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
        --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

function_env() {
    aws lambda get-function-configuration --function-name "$STACK_NAME" \
        --query "Environment.Variables.$1" --output text
}

# Prints Telegram's "description" and fails unless the response says ok
telegram_check() {
    python3 -c '
import json, sys
r = json.load(sys.stdin)
print("   Telegram:", r.get("description") or ("ok" if r.get("ok") else r))
sys.exit(0 if r.get("ok") else 1)'
}

# --- Build ---

build_package() {
    local tele="$REPO_DIR/submodules/telegram/tele.py"
    if [[ ! -f "$tele" ]]; then
        info "Fetching the telegram submodule"
        git -C "$REPO_DIR" submodule update --init || true
        [[ -f "$tele" ]] || fail "submodules/telegram/tele.py is missing. Run: git submodule update --init"
    fi

    info "Building the Lambda package"
    rm -rf "$BUILD_DIR"
    mkdir -p "$BUILD_DIR/pkg/submodules/telegram"

    # Linux ARM64 wheels for the Lambda runtime, whatever machine this runs on
    python3 -m pip install --quiet --disable-pip-version-check \
        --target "$BUILD_DIR/pkg" \
        --platform manylinux2014_aarch64 --platform manylinux_2_28_aarch64 \
        --implementation cp --python-version 3.12 --only-binary=:all: \
        -r "$SCRIPT_DIR/requirements.txt"

    cp "$REPO_DIR/watermarker.py" "$REPO_DIR/watermarker_core.py" "$REPO_DIR/sun.webp" \
       "$SCRIPT_DIR/lambda_function.py" "$BUILD_DIR/pkg/"
    cp "$tele" "$BUILD_DIR/pkg/submodules/telegram/"

    (cd "$BUILD_DIR/pkg" && zip -qr9 "$PACKAGE" . -x '*__pycache__*')
    info "Package ready: $(du -h "$PACKAGE" | cut -f1)"
}

# --- Commands ---

cmd_deploy() {
    local params=()
    local bot_token="${BOT_TOKEN_ARG:-${BOT_TOKEN:-}}"

    if stack_exists; then
        info "Updating stack '$STACK_NAME' (settings and bot token are kept unless you pass new ones)"
    else
        info "Creating stack '$STACK_NAME'"
        if [[ -z "$bot_token" ]]; then
            read -r -s -p "Telegram bot token (from @BotFather): " bot_token
            echo
        fi
        [[ -n "$bot_token" ]] || fail "a bot token is required for the first deploy"
        params+=("WebhookSecret=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')")
    fi
    if [[ -n "$bot_token" ]]; then params+=("BotToken=$bot_token"); fi
    if [[ "$ALLOWED_SET" == "true" ]]; then params+=("AllowedChatIds=$ALLOWED_CHAT_IDS"); fi
    if [[ -n "$MEMORY" ]]; then params+=("MemorySize=$MEMORY"); fi

    build_package

    info "Deploying AWS resources (takes 1-3 minutes the first time)"
    # Parameters left out keep their previous values
    local deploy_args=(--template-file "$SCRIPT_DIR/template.yaml" --stack-name "$STACK_NAME"
                       --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset)
    if [[ ${#params[@]} -gt 0 ]]; then
        deploy_args+=(--parameter-overrides "${params[@]}")
    fi
    aws cloudformation deploy "${deploy_args[@]}"

    info "Uploading the code"
    aws lambda update-function-code --function-name "$STACK_NAME" \
        --zip-file "fileb://$PACKAGE" > /dev/null
    aws lambda wait function-updated --function-name "$STACK_NAME"

    # Function URLs also need lambda:InvokeFunction, limited to calls made through the URL.
    # CloudFormation has no property for that condition yet, so it is added here.
    local err
    if ! err=$(aws lambda add-permission --function-name "$STACK_NAME" \
            --statement-id FunctionUrlInvokeFunction --action lambda:InvokeFunction \
            --principal '*' --invoked-via-function-url 2>&1 > /dev/null); then
        if [[ "$err" != *ResourceConflictException* ]]; then
            echo "$err" >&2
            fail "could not add the Function URL invoke permission. Update the AWS CLI to the latest version and re-run."
        fi
    fi

    cmd_attach
    info "Done. Send the bot a photo or a zip to try it. Follow the logs with: $0 logs"
}

cmd_attach() {
    stack_exists || fail "stack '$STACK_NAME' not found. Run '$0 deploy' first"
    local url token secret commands
    url=$(stack_output WebhookUrl)
    token=$(function_env BOT_TOKEN)
    secret=$(function_env WEBHOOK_SECRET)

    info "Checking that $url answers"
    local body="" attempt
    for attempt in 1 2 3 4 5 6; do
        body=$(curl -sS -X POST "$url" -H 'Content-Type: application/json' -d '{}' || true)
        [[ "$body" == "forbidden" ]] && break
        sleep 5
    done
    # Our own code answers "forbidden" to a request without the secret; AWS's own
    # 403 means the URL permissions are wrong
    [[ "$body" == "forbidden" ]] || fail "the function URL is not reachable yet (got: $body). Wait a minute and run '$0 attach'"

    info "Pointing the Telegram webhook at the Lambda"
    curl -sS -X POST "https://api.telegram.org/bot$token/setWebhook" \
        --data-urlencode "url=$url" \
        --data-urlencode "secret_token=$secret" \
        --data-urlencode 'allowed_updates=["message"]' \
        --data-urlencode "max_connections=10" | telegram_check \
        || fail "Telegram rejected the webhook"

    commands=$(python3 - "$REPO_DIR/watermarker.py" <<'EOF'
import ast, json, sys
tree = ast.parse(open(sys.argv[1]).read())
for node in tree.body:
    if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "BOT_COMMANDS" for t in node.targets):
        commands = ast.literal_eval(node.value)
print(json.dumps({"commands": [{"command": k, "description": v} for k, v in commands.items()]}))
EOF
)
    info "Setting the bot's command menu"
    curl -sS -X POST "https://api.telegram.org/bot$token/setMyCommands" \
        -H 'Content-Type: application/json' -d "$commands" | telegram_check || true

    echo "   Note: while the webhook is set, a polling copy of watermarker.py using the"
    echo "   same bot token gets errors from Telegram. Use '$0 detach' to switch back."
}

cmd_detach() {
    stack_exists || fail "stack '$STACK_NAME' not found"
    info "Removing the Telegram webhook (pending messages are kept for a polling bot)"
    curl -sS -X POST "https://api.telegram.org/bot$(function_env BOT_TOKEN)/deleteWebhook" | telegram_check
}

cmd_status() {
    stack_exists || fail "stack '$STACK_NAME' not found"
    echo "Stack:         $STACK_NAME"
    echo "Function:      $(stack_output FunctionName)"
    echo "Webhook URL:   $(stack_output WebhookUrl)"
    echo "State bucket:  $(stack_output StateBucketName)"
    echo "Allowed chats: $(function_env ALLOWED_CHAT_IDS)"
    echo "Telegram webhook info:"
    curl -sS "https://api.telegram.org/bot$(function_env BOT_TOKEN)/getWebhookInfo" | python3 -m json.tool
}

cmd_logs() {
    aws logs tail "/aws/lambda/$STACK_NAME" --follow --since 1h
}

cmd_remove() {
    stack_exists || fail "stack '$STACK_NAME' not found"
    local bucket
    bucket=$(stack_output StateBucketName)
    read -r -p "Delete stack '$STACK_NAME' and stop the bot's webhook? [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }

    cmd_detach || true
    info "Deleting the stack"
    aws cloudformation delete-stack --stack-name "$STACK_NAME"
    aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME"
    info "Stack deleted. The per-chat settings bucket was kept: $bucket"
    echo "   To delete it too: aws s3 rb s3://$bucket --force"
}

check_prerequisites
"cmd_$COMMAND"
