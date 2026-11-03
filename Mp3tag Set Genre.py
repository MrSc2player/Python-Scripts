#!/usr/bin/env python3
import os
from mutagen.easyid3 import EasyID3
import mutagen

GENRE = "Nightcore"
ZIEL_VERZEICHNIS = "/home/jan/Downloads/Cat Nightcore"

for root, dirs, files in os.walk(ZIEL_VERZEICHNIS):
    for filename in files:
        if filename.lower().endswith('.mp3'):
            full_path = os.path.join(root, filename)
            try:
                audio = EasyID3(full_path)
            except mutagen.id3.ID3NoHeaderError:
                mutagen.File(full_path, easy=True).add_tags()
                audio = EasyID3(full_path)
            audio['genre'] = GENRE
            audio.save()
            print(f"{full_path}: Genre gesetzt auf {GENRE}")

