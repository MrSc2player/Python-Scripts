#!/usr/bin/env python3
import glob
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, TXXX

# Für ausführung lokal im Ordner wo das Skript selber liegt entfällt die Definition des Musikordners und der Teil "for" bis zum ende sieht so aus for filename in glob.glob("*.mp3"):

#Pfad der Musikdateien
#musikordner = "/home/jan/Music/Nightcore/CAT Nightcore"

# Sofern das statt das obere ohne Raute ist werden alle Dateien in Musik Ordner betrachtet
musikordner = "/home/jan/Downloads"

for filename in glob.glob(f"{musikordner}/**/*.mp3", recursive=True):
    try:
        audio = EasyID3(filename)
        id3 = ID3(filename)
    except Exception as e:
        print(f"Fehler bei {filename}: {e}")
        continue

    # Various Artists entfernen
    for key in ['artist', 'albumartist']:
        if key in audio and audio[key][0] == "Various Artists":
            del audio[key]

    # Spezialfall: Album-Tag für release_type
    release_type = "album"
    if 'album' in audio and "[Standalone Recordings]" in audio['album'][0]:
        release_type = "single"

    # Frühere release status und release type löschen
    for frame in id3.getall("TXXX"):
        desc = frame.desc.lower()
        if desc == "release status" or desc == "release type":
            id3.delall("TXXX:" + frame.desc)

    # Setze neue Werte
    id3.add(TXXX(encoding=3, desc="release status", text="official"))
    id3.add(TXXX(encoding=3, desc="release type", text=release_type))

    # Tags speichern
    audio.save()
    id3.save()
    print(f"Bearbeitet: {filename}")


