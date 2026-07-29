ARG X11_TAG=ghcr.io/ankimcp/headless-anki:x11-vnc-v1.4.0
FROM ${X11_TAG}

ARG ANKI_VERSION=26.5
ARG ADDON_IDS=""

# Install addons if specified (version format: 26.5 -> 260500)
RUN ANKI_VER=$(echo $ANKI_VERSION | awk -F. '{printf "%02d%02d%02d", $1, $2, $3}') && \
    if [ -n "$ADDON_IDS" ]; then \
        for id in $ADDON_IDS; do \
            echo "Installing addon: $id" && \
            curl -sL -o /tmp/$id.zip "https://ankiweb.net/shared/download/$id?v=2.1&p=$ANKI_VER" && \
            mkdir -p /data/addons21/$id && \
            unzip -q /tmp/$id.zip -d /data/addons21/$id/ && \
            rm /tmp/$id.zip && \
            if [ "$id" = "2055492159" ]; then \
                echo "Configuring AnkiConnect: env-governed bind address + no auto-update" && \
                # Drop webBindAddress from config.json so AnkiConnect's DEFAULT_CONFIG
                # (which reads $ANKICONNECT_BIND_ADDRESS) governs the bind address.
                # A hardcoded value here would be re-shadowed by the /data volume or
                # reset by an addon auto-update; the env var is the stable hook. \
                jq 'del(.webBindAddress)' /data/addons21/$id/config.json > /tmp/config.json && \
                mv /tmp/config.json /data/addons21/$id/config.json && \
                # Disable auto-update so Anki cannot silently re-download the addon
                # and overwrite config.json with upstream defaults on startup. \
                printf '%s' '{"disabled": false, "update_enabled": false, "name": "AnkiConnect"}' \
                    > /data/addons21/$id/meta.json; \
            fi; \
        done; \
    fi && \
    chown -R anki /data/addons21
