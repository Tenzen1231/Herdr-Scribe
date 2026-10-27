#!/bin/bash
# Removes Herdr Стенограф, its settings, autostart entry, caches and permissions.
# Transcripts in ~/Documents/Стенограммы are kept.
set -uo pipefail

pkill -x HerdrScribe 2>/dev/null && sleep 1
rm -rf "$HOME/Applications/HerdrScribe.app" \
       "$HOME/Library/LaunchAgents/com.herdr.scribe.plist" \
       "$HOME/Library/Application Support/HerdrScribe" \
       "$HOME/Library/Caches/HerdrScribe-build" \
       "$HOME/Library/Caches/HerdrScribe-model"
tccutil reset All com.herdr.scribe >/dev/null 2>&1
printf 'Herdr Стенограф удалён. Стенограммы в папке «Документы/Стенограммы» сохранены.\n'
