#!/bin/bash

# Konfiguration
GOTIFY_URL=""  # Deine Gotify-URL
GOTIFY_TOKEN=""                 # Gotify App-Token
HOSTNAME=$(hostname)
PRIORITY=5

# Funktion für Gotify-Benachrichtigung
send_gotify() {
    curl -s -X POST "$GOTIFY_URL/message?token=$GOTIFY_TOKEN" \
        -F "title=$1" \
        -F "message=$2" \
        -F "priority=$PRIORITY" > /dev/null
}

# Log-Datei
LOGFILE="/var/log/apt-update-notify.log"
echo "$(date): Starte APT-Update-Check" >> "$LOGFILE"

# apt update ausführen
if apt update >> "$LOGFILE" 2>&1; then
    # Anzahl upgradable Pakete zählen (erste Zeile ist Header)
    UPDATE_COUNT=$(apt list --upgradable 2>/dev/null | grep -v "^Listing" | wc -l)
    
    if [ "$UPDATE_COUNT" -gt 0 ]; then
        UPDATES_LIST=$(apt list --upgradable 2>/dev/null | grep -v "^Listing")
        send_gotify "Updates verfügbar: $HOSTNAME" "$UPDATE_COUNT Updates gefunden:\n$UPDATES_LIST"
        echo "$(date): $UPDATE_COUNT Updates gefunden und benachrichtigt" >> "$LOGFILE"
    else
        send_gotify "Keine Updates: $HOSTNAME" "Keine Paket-Updates verfügbar."
        echo "$(date): Keine Updates gefunden" >> "$LOGFILE"
    fi
else
    send_gotify "Fehler bei APT-Update: $HOSTNAME" "apt-get update fehlgeschlagen. Siehe Log: $LOGFILE"
    echo "$(date): apt-get update fehlgeschlagen" >> "$LOGFILE"
fi
