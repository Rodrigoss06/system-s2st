#!/usr/bin/env bash
# =============================================================================
# SINCRO Engine v4 - Continuar infraestructura desde donde fallo
# =============================================================================
# Ejecuta esto si el script original fallo en el paso 4 (Container Apps).
# Detecta recursos existentes y solo crea lo que falta.
#
# Uso:
#   chmod +x scripts/azure-infra-resume.sh
#   ./scripts/azure-infra-resume.sh
# =============================================================================

set -euo pipefail

# ─── CONFIGURACION (mismos valores que el script original) ─────────────────
SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
LOCATION="westus"
RESOURCE_GROUP="sincro-v4-rg"
ACR_NAME="sincrov4acr"
ENVIRONMENT_NAME="sincro-v4-env"
WORKER_APP="sincro-worker"
DISPATCHER_APP="sincro-dispatcher"

# ─── DETECTAR LO QUE YA EXISTE ─────────────────────────────────────────────
echo ""
echo ">>> Verificando recursos existentes..."

RG_EXISTS="$(az group exists --name "$RESOURCE_GROUP")"
ACR_EXISTS="$(az acr check-name --name "$ACR_NAME" --query nameAvailable -o tsv 2>/dev/null || echo "true")"
# check-name devuelve true si el nombre ESTA DISPONIBLE (no existe)
if [ "$ACR_EXISTS" = "true" ]; then
    ACR_EXISTS="false"
else
    ACR_EXISTS="true"
fi

ENV_EXISTS="false"
WORKER_EXISTS="false"
DISPATCHER_EXISTS="false"

if [ "$RG_EXISTS" = "true" ]; then
    ENV_EXISTS="$(az containerapp env show -g "$RESOURCE_GROUP" -n "$ENVIRONMENT_NAME" --query name -o tsv 2>/dev/null || echo "")"
    WORKER_EXISTS="$(az containerapp show -g "$RESOURCE_GROUP" -n "$WORKER_APP" --query name -o tsv 2>/dev/null || echo "")"
    DISPATCHER_EXISTS="$(az containerapp show -g "$RESOURCE_GROUP" -n "$DISPATCHER_APP" --query name -o tsv 2>/dev/null || echo "")"
fi

echo "   Resource Group:    $([ "$RG_EXISTS" = "true" ] && echo "EXISTE" || echo "NO EXISTE")"
echo "   ACR:               $([ "$ACR_EXISTS" = "true" ] && echo "EXISTE" || echo "NO EXISTE")"
echo "   Environment:       $([ -n "$ENV_EXISTS" ] && echo "EXISTE" || echo "NO EXISTE")"
echo "   Worker App:        $([ -n "$WORKER_EXISTS" ] && echo "EXISTE" || echo "NO EXISTE")"
echo "   Dispatcher App:    $([ -n "$DISPATCHER_EXISTS" ] && echo "EXISTE" || echo "NO EXISTE")"

# ─── CREDENCIALES ──────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  CREDENCIALES DEL MOTOR"
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

# ─── OBTENER DATOS DEL ENVIRONMENT ─────────────────────────────────────────
ACR_LOGIN_SERVER="$(az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" --query loginServer -o tsv)"
ENV_FQDN="$(az containerapp env show \
    --resource-group "$RESOURCE_GROUP" \
    --name "$ENVIRONMENT_NAME" \
    --query properties.defaultDomain -o tsv)"

echo ""
echo "   ACR login server:  $ACR_LOGIN_SERVER"
echo "   Environment FQDN:  $ENV_FQDN"

# ═══════════════════════════════════════════════════════════════════════════
# WORKER
# ═══════════════════════════════════════════════════════════════════════════
if [ -n "$WORKER_EXISTS" ]; then
    echo ""
    echo ">>> Worker ya existe. Solo actualizo secretos y env vars..."
else
    echo ""
    echo ">>> Creando Container App: $WORKER_APP"

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

    echo "   Worker creado."
fi

# Secretos (funciona tanto en create como en update)
az containerapp secret set \
    --resource-group "$RESOURCE_GROUP" \
    --name "$WORKER_APP" \
    --secrets \
        "deepgram-api-key=$DEEPGRAM_API_KEY" \
        "groq-api-key=$GROQ_API_KEY" \
        "fish-api-key=$FISH_API_KEY" \
        "sincro-token-secret=$SINCRO_TOKEN_SECRET"

# Variables de entorno
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

echo "   Worker listo."

# ═══════════════════════════════════════════════════════════════════════════
# DISPATCHER
# ═══════════════════════════════════════════════════════════════════════════
WORKER_FQDN="${WORKER_APP}.${ENV_FQDN}"
WORKER_WS_URL="wss://${WORKER_FQDN}/v1/stream"

if [ -n "$DISPATCHER_EXISTS" ]; then
    echo ""
    echo ">>> Dispatcher ya existe. Solo actualizo secretos y env vars..."
else
    echo ""
    echo ">>> Creando Container App: $DISPATCHER_APP"

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

    echo "   Dispatcher creado."
fi

az containerapp secret set \
    --resource-group "$RESOURCE_GROUP" \
    --name "$DISPATCHER_APP" \
    --secrets \
        "deepgram-api-key=$DEEPGRAM_API_KEY" \
        "groq-api-key=$GROQ_API_KEY" \
        "fish-api-key=$FISH_API_KEY" \
        "sincro-token-secret=$SINCRO_TOKEN_SECRET"

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

echo "   Dispatcher listo."

# ═══════════════════════════════════════════════════════════════════════════
# SERVICE PRINCIPAL (solo si no existe ya)
# ═══════════════════════════════════════════════════════════════════════════
SP_NAME="sincro-cicd-sp"
SP_EXISTS="$(az ad sp list --display-name "$SP_NAME" --query "[0].appId" -o tsv 2>/dev/null || echo "")"

if [ -n "$SP_EXISTS" ]; then
    echo ""
    echo ">>> Service Principal '$SP_NAME' ya existe (appId: $SP_EXISTS)."
    echo "    Si necesitas el JSON de credenciales, resetea las credenciales con:"
    echo "    az ad sp credential reset --id $SP_EXISTS --json-auth"
else
    echo ""
    echo ">>> Creando Service Principal para GitHub Actions"

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

    SP_APP_ID="$(echo "$SP_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['clientId'])")"

    az role assignment create \
        --assignee "$SP_APP_ID" \
        --role AcrPush \
        --scope "$(az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" --query id -o tsv)"

    echo "   Permisos de ACR asignados."
fi

# ═══════════════════════════════════════════════════════════════════════════
# RESUMEN FINAL
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  INFRAESTRUCTURA COMPLETA"
echo "══════════════════════════════════════════════════════════════"
echo ""
echo "  Resource Group:     $RESOURCE_GROUP"
echo "  Region:             $LOCATION"
echo "  ACR:                $ACR_NAME.azurecr.io"
echo "  Environment:        $ENVIRONMENT_NAME"
echo "  Worker:             https://${WORKER_APP}.${ENV_FQDN}"
echo "  Worker WS:          wss://${WORKER_FQDN}/v1/stream"
echo "  Dispatcher:         https://${DISPATCHER_APP}.${ENV_FQDN}"
echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  CONFIGURACION DE GITHUB"
echo "══════════════════════════════════════════════════════════════"
echo ""
echo "  Ve a: https://github.com/<tu-usuario>/<tu-repo>/settings/secrets/actions"
echo ""
echo "  Secrets:"
echo "    AZURE_CREDENTIALS         = <el JSON del service principal>"
echo "    AZURE_CLIENT_ID           = <clientId del JSON>"
echo "    AZURE_CLIENT_SECRET       = <clientSecret del JSON>"
echo ""
echo "  Variables:"
echo "    AZURE_RESOURCE_GROUP      = $RESOURCE_GROUP"
echo "    AZURE_ACR_NAME            = $ACR_NAME"
echo "    AZURE_WORKER_APP          = $WORKER_APP"
echo "    AZURE_DISPATCHER_APP      = $DISPATCHER_APP"
echo ""
echo "══════════════════════════════════════════════════════════════"