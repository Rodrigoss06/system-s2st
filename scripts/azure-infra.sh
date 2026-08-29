#!/usr/bin/env bash
# =============================================================================
# SINCRO Engine v4 - Infraestructura Azure completa
# =============================================================================
# Ejecuta este script UNA VEZ para crear todos los recursos en Azure.
# Requisitos:
#   - az CLI instalado y logueado (az login)
#   - Una suscripcion activa (az account show)
#   - Tener permisos de Contributor en la suscripcion
#
# Uso:
#   chmod +x scripts/azure-infra.sh
#   ./scripts/azure-infra.sh
#
# O paso a paso (lee el script y ejecuta cada bloque por separado).
# =============================================================================

set -euo pipefail

# ─── CONFIGURACION ──────────────────────────────────────────────────────────
# Cambia estos valores segun tu entorno. El resto se deriva de aqui.

SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
LOCATION="westus"                    # West US
RESOURCE_GROUP="sincro-v4-rg"
ACR_NAME="sincrov4acr"               # Azure Container Registry (sin .azurecr.io)
ENVIRONMENT_NAME="sincro-v4-env"     # Container Apps Environment
WORKER_APP="sincro-worker"           # Container App del Worker (WebSocket)
DISPATCHER_APP="sincro-dispatcher"   # Container App del Dispatcher (FastAPI)

# ─── CREDENCIALES DEL MOTOR ─────────────────────────────────────────────────
# Pide las credenciales UNA vez. Se guardan como secretos en las Container Apps.
# NO se hardcodean en el script ni en el repositorio.

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  CREDENCIALES DEL MOTOR"
echo "  Las necesitas para crear los secretos en las Container Apps."
echo "══════════════════════════════════════════════════════════════"
echo ""

read -r -p "Deepgram API Key: " DEEPGRAM_API_KEY
read -r -p "Groq API Key: " GROQ_API_KEY
read -r -p "Fish Audio API Key: " FISH_API_KEY
read -r -p "Token Secret (genera uno con: openssl rand -hex 32): " SINCRO_TOKEN_SECRET

if [[ -z "$DEEPGRAM_API_KEY" || -z "$GROQ_API_KEY" || -z "$FISH_API_KEY" || -z "$SINCRO_TOKEN_SECRET" ]]; then
    echo "ERROR: Las cuatro credenciales son obligatorias."
    exit 1
fi

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  RESUMEN"
echo "══════════════════════════════════════════════════════════════"
echo "  Suscripcion:       $SUBSCRIPTION_ID"
echo "  Region:            $LOCATION"
echo "  Resource Group:    $RESOURCE_GROUP"
echo "  ACR:               $ACR_NAME"
echo "  Environment:       $ENVIRONMENT_NAME"
echo "  Worker App:        $WORKER_APP"
echo "  Dispatcher App:    $DISPATCHER_APP"
echo "══════════════════════════════════════════════════════════════"
echo ""
read -r -p "¿Crear todo con estos valores? [s/N]: " CONFIRM
if [[ ! "$CONFIRM" =~ ^[sS]$ ]]; then
    echo "Cancelado."
    exit 0
fi

# ═══════════════════════════════════════════════════════════════════════════
# PASO 1: Resource Group
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo ">>> [1/8] Creando Resource Group: $RESOURCE_GROUP en $LOCATION"

az group create \
    --name "$RESOURCE_GROUP" \
    --location "$LOCATION"

echo "   Hecho."

# ═══════════════════════════════════════════════════════════════════════════
# PASO 2: Azure Container Registry
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo ">>> [2/8] Creando Azure Container Registry: $ACR_NAME"

az acr create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$ACR_NAME" \
    --sku Basic \
    --admin-enabled true

ACR_LOGIN_SERVER="$(az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" --query loginServer -o tsv)"
echo "   ACR login server: $ACR_LOGIN_SERVER"

# ═══════════════════════════════════════════════════════════════════════════
# PASO 3: Container Apps Environment
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo ">>> [3/8] Creando Container Apps Environment: $ENVIRONMENT_NAME"

az containerapp env create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$ENVIRONMENT_NAME" \
    --location "$LOCATION"

ENV_FQDN="$(az containerapp env show \
    --resource-group "$RESOURCE_GROUP" \
    --name "$ENVIRONMENT_NAME" \
    --query properties.defaultDomain -o tsv)"

echo "   Environment FQDN: $ENV_FQDN"

# ═══════════════════════════════════════════════════════════════════════════
# PASO 4: Container App - Worker (WebSocket)
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo ">>> [4/8] Creando Container App: $WORKER_APP"

# IMPORTANTE: no se pueden usar secretref en --env-vars durante el create
# porque los secretos no existen todavia. Se crea la app sin secretos,
# se añaden los secretos, y luego se actualizan las env vars.
az containerapp create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$WORKER_APP" \
    --environment "$ENVIRONMENT_NAME" \
    --registry-server "$ACR_LOGIN_SERVER" \
    --registry-identity system \
    --image "mcr.microsoft.com/k8se/quickstart:latest" \
    --target-port 8080 \
    --ingress external \
    --transport http2 \
    --min-replicas 1 \
    --max-replicas 3 \
    --cpu 2.0 \
    --memory 4.0Gi

# Primero los secretos...
az containerapp secret set \
    --resource-group "$RESOURCE_GROUP" \
    --name "$WORKER_APP" \
    --secrets \
        "deepgram-api-key=$DEEPGRAM_API_KEY" \
        "groq-api-key=$GROQ_API_KEY" \
        "fish-api-key=$FISH_API_KEY" \
        "sincro-token-secret=$SINCRO_TOKEN_SECRET"

# ...luego las variables de entorno que los referencian
az containerapp update \
    --resource-group "$RESOURCE_GROUP" \
    --name "$WORKER_APP" \
    --set-env-vars \
        "DEEPGRAM_API_KEY=secretref:deepgram-api-key" \
        "GROQ_API_KEY=secretref:groq-api-key" \
        "FISH_API_KEY=secretref:fish-api-key" \
        "SINCRO_TOKEN_SECRET=secretref:sincro-token-secret" \
        "SINCRO_SRC_LANG=es" \
        "SINCRO_DST_LANG=en" \
        "SINCRO_LLM_MODEL=openai/gpt-oss-120b" \
        "SINCRO_LLM_REASONING_EFFORT=low" \
        "SINCRO_LLM_TEMPERATURE=0.2" \
        "SINCRO_LLM_MAX_TOKENS=800" \
        "SINCRO_TTS_MODEL=s2.1-pro" \
        "SINCRO_LOG_LEVEL=INFO"

echo "   Worker creado."

# ═══════════════════════════════════════════════════════════════════════════
# PASO 5: Container App - Dispatcher (FastAPI)
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo ">>> [5/8] Creando Container App: $DISPATCHER_APP"

# Construir la URL del Worker para el Dispatcher
WORKER_FQDN="${WORKER_APP}.${ENV_FQDN}"
WORKER_WS_URL="wss://${WORKER_FQDN}/v1/stream"

az containerapp create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$DISPATCHER_APP" \
    --environment "$ENVIRONMENT_NAME" \
    --registry-server "$ACR_LOGIN_SERVER" \
    --registry-identity system \
    --image "mcr.microsoft.com/k8se/quickstart:latest" \
    --target-port 8000 \
    --ingress external \
    --transport auto \
    --min-replicas 1 \
    --max-replicas 2 \
    --cpu 1.0 \
    --memory 2.0Gi

# Primero los secretos...
az containerapp secret set \
    --resource-group "$RESOURCE_GROUP" \
    --name "$DISPATCHER_APP" \
    --secrets \
        "deepgram-api-key=$DEEPGRAM_API_KEY" \
        "groq-api-key=$GROQ_API_KEY" \
        "fish-api-key=$FISH_API_KEY" \
        "sincro-token-secret=$SINCRO_TOKEN_SECRET"

# ...luego las variables de entorno que los referencian
az containerapp update \
    --resource-group "$RESOURCE_GROUP" \
    --name "$DISPATCHER_APP" \
    --set-env-vars \
        "DEEPGRAM_API_KEY=secretref:deepgram-api-key" \
        "GROQ_API_KEY=secretref:groq-api-key" \
        "FISH_API_KEY=secretref:fish-api-key" \
        "SINCRO_TOKEN_SECRET=secretref:sincro-token-secret" \
        "SINCRO_WORKER_WS_URL=$WORKER_WS_URL" \
        "SINCRO_VOICES_DB=/app/data/voices.db" \
        "SINCRO_LOG_LEVEL=INFO"

echo "   Dispatcher creado."

# ═══════════════════════════════════════════════════════════════════════════
# PASO 6: Service Principal para GitHub Actions
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo ">>> [6/8] Creando Service Principal para GitHub Actions"

SP_NAME="sincro-cicd-sp"
SP_JSON="$(az ad sp create-for-rbac \
    --name "$SP_NAME" \
    --role contributor \
    --scopes "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP" \
    --json-auth)"

echo ""
echo "   ╔══════════════════════════════════════════════════════════════╗"
echo "   ║  GUARDA ESTE JSON. NO SE MOSTRARA OTRA VEZ.                ║"
echo "   ║  Va en GitHub → Settings → Secrets → AZURE_CREDENTIALS     ║"
echo "   ╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "$SP_JSON"
echo ""

# ═══════════════════════════════════════════════════════════════════════════
# PASO 7: Asignar permisos de ACR al Service Principal
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo ">>> [7/8] Asignando permisos de ACR pull al Service Principal"

SP_APP_ID="$(echo "$SP_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['clientId'])")"

az acr update \
    --name "$ACR_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --admin-enabled false

# El SP necesita AcrPush para el workflow (push de imagen desde GitHub)
az role assignment create \
    --assignee "$SP_APP_ID" \
    --role AcrPush \
    --scope "$(az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" --query id -o tsv)"

echo "   Permisos asignados."

# ═══════════════════════════════════════════════════════════════════════════
# PASO 8: Resumen final
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo "══════════════════════════════════════════════════════════════════"
echo "  INFRAESTRUCTURA CREADA"
echo "══════════════════════════════════════════════════════════════════"
echo ""
echo "  Resource Group:     $RESOURCE_GROUP"
echo "  Region:             $LOCATION"
echo "  ACR:                $ACR_NAME.azurecr.io"
echo "  Environment:        $ENVIRONMENT_NAME"
echo "  Worker:             https://${WORKER_APP}.${ENV_FQDN}"
echo "  Worker WS:          wss://${WORKER_FQDN}/v1/stream"
echo "  Dispatcher:         https://${DISPATCHER_APP}.${ENV_FQDN}"
echo ""
echo "══════════════════════════════════════════════════════════════════"
echo "  CONFIGURACION DE GITHUB (siguiente paso)"
echo "══════════════════════════════════════════════════════════════════"
echo ""
echo "  Ve a: https://github.com/<tu-usuario>/<tu-repo>/settings/secrets/actions"
echo ""
echo "  Secrets:"
echo "    AZURE_CREDENTIALS         = <el JSON del paso 6>"
echo ""
echo "  Variables:"
echo "    AZURE_RESOURCE_GROUP      = $RESOURCE_GROUP"
echo "    AZURE_ACR_NAME            = $ACR_NAME"
echo "    AZURE_WORKER_APP          = $WORKER_APP"
echo "    AZURE_DISPATCHER_APP      = $DISPATCHER_APP"
echo "    AZURE_CONTAINER_APP_ENV   = $ENVIRONMENT_NAME"
echo ""
echo "══════════════════════════════════════════════════════════════════"
echo ""
echo "  Despues de configurar GitHub, haz un push a main."
echo "  El workflow construye la imagen, la sube al ACR y actualiza"
echo "  las Container Apps automaticamente."
echo ""