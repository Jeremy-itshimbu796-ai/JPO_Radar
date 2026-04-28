"""
Interface Radar - Style militaire + Assistant Vocal Professionnel
Compatible Arduino (port série) ou mode démo

Dépendances :
     pip install pygame pyserial
     (voix Gemini TTS via API — définir GEMINI_API_KEY)

Lancer : python interface.py
"""

import base64
import io
import json
import math
import os
import queue
import random
import sys
import threading
import time
import urllib.error
import urllib.request

import pygame

# ╔══════════════════════════════════════════════════════════╗
# ║                        CONFIG                           ║
# ╚══════════════════════════════════════════════════════════╝

LARGEUR, HAUTEUR = 960, 960
FPS              = 60
TITRE            = "RADAR SYSTEM v2.0"

# ── Palette militaire ──────────────────────────────────────
NOIR        = (0,   0,   0)
VERT_FORT   = (0,   255, 70)
VERT_MOY    = (0,   180, 50)
VERT_FAIBLE = (0,   80,  20)
VERT_FOND   = (0,   20,  5)
ROUGE       = (255, 60,  60)
ROUGE_VIF   = (255, 30,  30)
ORANGE      = (255, 140, 0)
JAUNE       = (255, 220, 0)
BLANC       = (200, 220, 200)
GRIS        = (60,  80,  60)
CYAN        = (0,   220, 220)

# ── Géométrie ─────────────────────────────────────────────
CENTRE_X        = LARGEUR // 2
CENTRE_Y        = HAUTEUR // 2
RAYON           = 390
NB_CERCLES      = 4
DISTANCE_MAX_CM = 100

# ── Animation ─────────────────────────────────────────────
VITESSE_DEG     = 1.5
LONGUEUR_TRAINE = 90
DUREE_POINT     = 5.0

# ── Pause radar sur détection ──────────────────────────────
DUREE_PAUSE_S   = 3.5         # secondes d'arrêt après détection

# ── Assistant vocal ────────────────────────────────────────
COOLDOWN_VOCAL_S = 6.0        # secondes min entre deux alertes par secteur
NB_SECTEURS      = 8
MAX_LOGS         = 6

# ── Gemini TTS ──────────────────────────────────────────────
GEMINI_API_KEY_ENV            = "GEMINI_API_KEY"
GEMINI_TTS_MODEL              = "gemini-2.5-flash-preview-tts"
GEMINI_TTS_VOICE              = ""       # ex: "Kore" pour test; choisir une voix FR disponible
GEMINI_TTS_AUDIO_MIME         = "audio/wav"
GEMINI_TTS_REQUEST_TIMEOUT_S  = 20
# Endpoint v1beta (API Gemini susceptible d'évoluer).
GEMINI_TTS_ENDPOINT           = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

# ── Mode ──────────────────────────────────────────────────
MODE       = "demo"           # "demo" ou "arduino"
PORT_SERIE = "COM3"
BAUD_RATE  = 9600


# ╔══════════════════════════════════════════════════════════╗
# ║        ASSISTANT VOCAL — Gemini TTS API                 ║
# ║                                                         ║
# ║  • Nécessite une clé GEMINI_API_KEY                     ║
# ║  • Messages militaires professionnels et variés         ║
# ║  • Pause du radar pendant la lecture                    ║
# ║  • Repli silencieux si l'API est indisponible            ║
# ╚══════════════════════════════════════════════════════════╝

class VoiceAssistant(threading.Thread):

    # ── Phrases d'alerte professionnelles (tirées aléatoirement) ──
    _TEMPLATES = [
        "Attention. Contact détecté. Secteur {dir}. Distance estimée {dist} mètres.",
        "Alerte. Objet identifié. Direction {dir}. À {dist} mètres.",
        "Contact. Secteur {dir}. Proximité {dist} mètres. Surveillance active.",
        "Signal reçu. Cible localisée, secteur {dir}, distance {dist} mètres.",
        "Avertissement. Intrusion détectée. Zone {dir}. Distance {dist} mètres.",
        "Radar confirme contact. Secteur {dir}. Distance {dist} mètres.",
    ]

    _MSG_DEMARRAGE = (
        "Système radar initialisé. "
        "Assistant vocal opérationnel. "
        "Surveillance en cours."
    )

    _MSG_EXTINCTION = "Système radar hors ligne. Fin de la surveillance."

    def __init__(self):
        super().__init__(daemon=True, name="VoiceThread")
        self._queue     : queue.Queue = queue.Queue()
        self._actif     = True
        self._pret      = threading.Event()
        self._ok        = False
        self._cooldowns : dict[int, float] = {}
        self._api_key   = os.getenv(GEMINI_API_KEY_ENV, "")
        self._model     = os.getenv("GEMINI_TTS_MODEL", GEMINI_TTS_MODEL)
        self._voice     = os.getenv("GEMINI_TTS_VOICE", GEMINI_TTS_VOICE)
        self._mime      = os.getenv("GEMINI_TTS_AUDIO_MIME", GEMINI_TTS_AUDIO_MIME)
        self._audio_ok  = False
        self._channel   = None

    # ── Propriété publique ────────────────────────────────

    @property
    def pret(self) -> bool:
        return self._ok

    # ── API publique ──────────────────────────────────────

    def annoncer_detection(self, angle_deg: float, distance_ratio: float):
        """Enfile une alerte si le cooldown du secteur est écoulé."""
        secteur = int(angle_deg / (360 / NB_SECTEURS)) % NB_SECTEURS
        now     = time.time()
        if now - self._cooldowns.get(secteur, 0) < COOLDOWN_VOCAL_S:
            return
        self._cooldowns[secteur] = now

        dist_m    = round(distance_ratio * DISTANCE_MAX_CM / 100, 1)
        dist_str  = f"{dist_m:.1f}".replace(".", " virgule ")
        direction = self._angle_vers_direction(angle_deg)
        template  = random.choice(self._TEMPLATES)
        texte     = template.format(dir=direction, dist=dist_str)
        self._queue.put(texte)

    def stop(self):
        self._actif = False
        if self._channel:
            self._channel.stop()
        self._queue.put(("__EXTINCTION__", self._MSG_EXTINCTION))

    # ── Corps du thread ───────────────────────────────────

    def run(self):
        if not self._api_key:
            print("[VOCAL] ❌ GEMINI_API_KEY manquant — radar sans son.")
            self._pret.set()
            return

        if not self._init_audio():
            print("[VOCAL] ❌ Audio indisponible — radar sans son.")
            self._pret.set()
            return

        # Test d'initialisation
        ok = self._tts(self._MSG_DEMARRAGE, timeout=15)
        self._ok = ok
        self._pret.set()

        if not ok:
            print("[VOCAL] ❌ Gemini TTS indisponible — radar sans son.")
            return

        print("[VOCAL] ✔ Assistant vocal prêt")

        while self._actif:
            item = self._queue.get()
            if item is None:
                break

            # Tuple spécial pour l'extinction
            if isinstance(item, tuple) and item[0] == "__EXTINCTION__":
                self._tts(item[1], timeout=8)
                break

            print(f"[VOCAL] >> {item}")
            self._tts(item, timeout=15)

    # ── Moteur TTS : Gemini API ─────────────────────────

    def _tts(self, texte: str, timeout: int = 15) -> bool:
        """
        Lit `texte` via Gemini TTS.
        Retourne True si la lecture s'est bien déroulée.
        """
        if not self._api_key:
            return False

        if not self._init_audio():
            return False

        audio = self._requete_tts(texte, timeout=GEMINI_TTS_REQUEST_TIMEOUT_S)
        if not audio:
            return False
        return self._lire_audio(audio, timeout=timeout)

    def _init_audio(self) -> bool:
        if self._audio_ok:
            return True
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init()
            self._audio_ok = True
            return True
        except pygame.error as e:
            print(f"[VOCAL] Erreur audio pygame : {e}")
            return False

    def _requete_tts(self, texte: str, timeout: int) -> bytes | None:
        payload = {
            "contents": [
                {"role": "user", "parts": [{"text": texte}]},
            ],
            "generationConfig": {
                "responseMimeType": self._mime,
            },
        }
        if self._voice:
            payload["generationConfig"]["speechConfig"] = {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {
                        "voiceName": self._voice,
                    }
                }
            }

        request = urllib.request.Request(
            GEMINI_TTS_ENDPOINT.format(model=self._model),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self._api_key,
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_json = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8")
            except Exception:
                detail = str(e)
            print(f"[VOCAL] Erreur Gemini TTS HTTP : {detail}")
            return None
        except urllib.error.URLError as e:
            print(f"[VOCAL] Erreur Gemini TTS réseau : {e}")
            return None
        except Exception as e:
            print(f"[VOCAL] Erreur Gemini TTS : {e}")
            return None

        audio = self._extraire_audio(response_json)
        if not audio:
            error_msg = response_json.get("error", {}).get("message")
            if error_msg:
                print(f"[VOCAL] Réponse Gemini TTS invalide : {error_msg}")
            else:
                print("[VOCAL] Réponse Gemini TTS sans audio.")
        return audio

    @staticmethod
    def _extraire_audio(response_json: dict) -> bytes | None:
        for candidate in response_json.get("candidates", []):
            content = candidate.get("content", {})
            for part in content.get("parts", []):
                # Compatibilité: certaines réponses utilisent inlineData, d'autres inline_data.
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and "data" in inline:
                    try:
                        return base64.b64decode(inline["data"])
                    except Exception:
                        return None
        return None

    def _lire_audio(self, audio_bytes: bytes, timeout: int) -> bool:
        try:
            try:
                sound = pygame.mixer.Sound(buffer=audio_bytes)
            except pygame.error:
                # Anciennes versions de pygame attendent un fichier-like.
                sound = pygame.mixer.Sound(file=io.BytesIO(audio_bytes))
            self._channel = sound.play()
            if not self._channel:
                return False
            start = time.time()
            while self._channel.get_busy():
                if time.time() - start > timeout:
                    self._channel.stop()
                    return False
                time.sleep(0.05)
            return True
        except pygame.error as e:
            print(f"[VOCAL] Erreur lecture audio : {e}")
            return False

    # ── Direction cardinale ───────────────────────────────

    @staticmethod
    def _angle_vers_direction(angle: float) -> str:
        seuils = [
            (22.5,  "Nord"),
            (67.5,  "Nord Est"),
            (112.5, "Est"),
            (157.5, "Sud Est"),
            (202.5, "Sud"),
            (247.5, "Sud Ouest"),
            (292.5, "Ouest"),
            (337.5, "Nord Ouest"),
            (360.0, "Nord"),
        ]
        a = angle % 360
        for seuil, label in seuils:
            if a < seuil:
                return label
        return "Nord"


# ╔══════════════════════════════════════════════════════════╗
# ║                   JOURNAL DE DÉTECTIONS                 ║
# ╚══════════════════════════════════════════════════════════╝

class DetectionLog:
    def __init__(self, max_entries: int = MAX_LOGS):
        self._entries: list[dict] = []
        self._max = max_entries

    def ajouter(self, angle: float, distance_ratio: float):
        direction = VoiceAssistant._angle_vers_direction(angle)
        dist_m    = distance_ratio * DISTANCE_MAX_CM / 100
        self._entries.insert(0, {
            "texte": f"{time.strftime('%H:%M:%S')}  {direction:<12} {dist_m:.1f} m",
            "t0":    time.time(),
        })
        self._entries = self._entries[:self._max]

    def dessiner(self, surface: pygame.Surface, font: pygame.font.Font):
        x, y0 = 14, 60
        surface.blit(font.render("── CONTACTS ──────────────", True, VERT_MOY), (x, y0))
        for i, entry in enumerate(self._entries):
            age   = time.time() - entry["t0"]
            alpha = max(0.25, 1.0 - age / 20.0)
            g     = int(200 * alpha)
            r     = int(80  * alpha) if i == 0 else 0
            txt   = font.render(entry["texte"], True, (r, g, int(g * 0.3)))
            surface.blit(txt, (x, y0 + 18 + i * 16))


# ╔══════════════════════════════════════════════════════════╗
# ║                     POINT DÉTECTÉ                       ║
# ╚══════════════════════════════════════════════════════════╝

class PointDetecte:
    def __init__(self, angle_deg: float, distance_ratio: float):
        self.angle = angle_deg
        self.dist  = distance_ratio
        self.t0    = time.time()
        rad    = math.radians(angle_deg - 90)
        self.x = int(CENTRE_X + distance_ratio * RAYON * math.cos(rad))
        self.y = int(CENTRE_Y + distance_ratio * RAYON * math.sin(rad))

    def vivant(self) -> bool:
        return (time.time() - self.t0) < DUREE_POINT

    def alpha(self) -> float:
        return max(0.0, 1.0 - (time.time() - self.t0) / DUREE_POINT)


# ╔══════════════════════════════════════════════════════════╗
# ║                   FLASH D'ALERTE                        ║
# ╚══════════════════════════════════════════════════════════╝

class AlerteFlash:
    def __init__(self):
        self._t0    = -99.0
        self._duree = 0.7

    def declencher(self):
        self._t0 = time.time()

    def dessiner(self, surface: pygame.Surface):
        age = time.time() - self._t0
        if age > self._duree:
            return
        alpha   = int(140 * (1.0 - age / self._duree))
        overlay = pygame.Surface((LARGEUR, HAUTEUR), pygame.SRCALPHA)
        pygame.draw.circle(overlay, (255, 0, 0, alpha), (CENTRE_X, CENTRE_Y), RAYON)
        surface.blit(overlay, (0, 0))


# ╔══════════════════════════════════════════════════════════╗
# ║               FONCTIONS DE RENDU RADAR                  ║
# ╚══════════════════════════════════════════════════════════╝

def angle_vers_xy(angle_deg: float, rayon_px: int) -> tuple:
    rad = math.radians(angle_deg - 90)
    return (
        int(CENTRE_X + rayon_px * math.cos(rad)),
        int(CENTRE_Y + rayon_px * math.sin(rad)),
    )


def dessiner_grille(surface: pygame.Surface, font_sm: pygame.font.Font):
    pygame.draw.circle(surface, VERT_FOND, (CENTRE_X, CENTRE_Y), RAYON)
    for i in range(1, NB_CERCLES + 1):
        r = int(RAYON * i / NB_CERCLES)
        pygame.draw.circle(surface, VERT_FAIBLE, (CENTRE_X, CENTRE_Y), r, 1)
        label = font_sm.render(
            f"{i * (DISTANCE_MAX_CM // NB_CERCLES)} m", True, VERT_FAIBLE)
        surface.blit(label, (CENTRE_X + r + 4, CENTRE_Y - 12))
    for deg in range(0, 360, 30):
        x2, y2  = angle_vers_xy(deg, RAYON)
        couleur = VERT_MOY if deg % 90 == 0 else VERT_FAIBLE
        pygame.draw.line(surface, couleur, (CENTRE_X, CENTRE_Y), (x2, y2), 1)
    pygame.draw.circle(surface, VERT_MOY, (CENTRE_X, CENTRE_Y), RAYON, 2)
    for deg, label_txt in [(0, "N"), (90, "E"), (180, "S"), (270, "O")]:
        x, y = angle_vers_xy(deg, RAYON + 22)
        txt  = font_sm.render(label_txt, True, CYAN)
        surface.blit(txt, (x - txt.get_width() // 2, y - txt.get_height() // 2))


def dessiner_traine(surface: pygame.Surface, angle_actuel: float):
    nb = 70
    for i in range(nb):
        a         = (angle_actuel - i * LONGUEUR_TRAINE / nb) % 360
        ratio     = 1.0 - i / nb
        intensite = int(170 * ratio)
        couleur   = (0, intensite, int(intensite * 0.25))
        x2, y2   = angle_vers_xy(a, RAYON - 1)
        pygame.draw.line(surface, couleur, (CENTRE_X, CENTRE_Y), (x2, y2), 2)


def dessiner_ligne(surface: pygame.Surface, angle_actuel: float):
    x2, y2 = angle_vers_xy(angle_actuel, RAYON)
    pygame.draw.line(surface, VERT_FORT, (CENTRE_X, CENTRE_Y), (x2, y2), 2)
    pygame.draw.circle(surface, BLANC, (x2, y2), 3)


def dessiner_points(surface: pygame.Surface, points: list):
    for p in points:
        if not p.vivant():
            continue
        a        = p.alpha()
        couleur  = (int(255 * a), int(60 * a), 0)
        rayon_pt = max(3, int(7 * a))
        pygame.draw.circle(surface, couleur, (p.x, p.y), rayon_pt)
        halo = pygame.Surface((rayon_pt * 4, rayon_pt * 4), pygame.SRCALPHA)
        pygame.draw.circle(halo, (*couleur, int(70 * a)),
                           (rayon_pt * 2, rayon_pt * 2), rayon_pt * 2)
        surface.blit(halo, (p.x - rayon_pt * 2, p.y - rayon_pt * 2))


def dessiner_hud(
    surface:   pygame.Surface,
    font_t:    pygame.font.Font,
    font_sm:   pygame.font.Font,
    angle:     float,
    nb_points: int,
    fps:       float,
    vocal_ok:  bool,
    en_pause:  bool,
    tps_pause: float,
):
    # ── Bandeau haut ──────────────────────────────────────
    pygame.draw.rect(surface, (0, 15, 5), (0, 0, LARGEUR, 50))
    pygame.draw.line(surface, VERT_MOY, (0, 50), (LARGEUR, 50), 1)
    surface.blit(font_t.render(TITRE, True, VERT_FORT), (18, 14))

    mode_color = JAUNE if MODE == "arduino" else VERT_MOY
    surface.blit(font_sm.render(
        f"MODE: {'ARDUINO' if MODE == 'arduino' else 'DEMO'}", True, mode_color),
        (LARGEUR - 210, 10))
    surface.blit(font_sm.render(f"FPS : {int(fps)}", True, GRIS), (LARGEUR - 210, 28))

    vocal_label = "VOCAL : ON" if vocal_ok else "VOCAL : OFF"
    vocal_color = CYAN if vocal_ok else ROUGE
    surface.blit(font_sm.render(vocal_label, True, vocal_color), (LARGEUR // 2 - 45, 18))

    # ── Bandeau bas ───────────────────────────────────────
    pygame.draw.rect(surface, (0, 15, 5), (0, HAUTEUR - 50, LARGEUR, 50))
    pygame.draw.line(surface, VERT_MOY, (0, HAUTEUR - 50), (LARGEUR, HAUTEUR - 50), 1)

    surface.blit(font_sm.render(f"ANGLE : {int(angle):>3} deg", True, BLANC),
                 (18, HAUTEUR - 34))

    obj_color = ROUGE_VIF if nb_points > 0 else VERT_MOY
    surface.blit(font_sm.render(f"CONTACTS ACTIFS : {nb_points}", True, obj_color),
                 (LARGEUR // 2 - 80, HAUTEUR - 34))

    surface.blit(font_sm.render(time.strftime("%H:%M:%S"), True, GRIS),
                 (LARGEUR - 110, HAUTEUR - 34))

    # ── Bannière ANALYSE EN COURS pendant la pause ────────
    if en_pause:
        msg  = f"ANALYSE EN COURS  —  reprise dans {tps_pause:.1f} s"
        txt  = font_t.render(msg, True, ORANGE)
        tx   = CENTRE_X - txt.get_width() // 2
        ty   = CENTRE_Y + RAYON + 12
        bg   = pygame.Surface((txt.get_width() + 24, txt.get_height() + 10), pygame.SRCALPHA)
        bg.fill((40, 20, 0, 200))
        surface.blit(bg, (tx - 12, ty - 5))
        surface.blit(txt, (tx, ty))

    # ── Réticule ─────────────────────────────────────────
    pygame.draw.circle(surface, VERT_MOY,  (CENTRE_X, CENTRE_Y), 6, 1)
    pygame.draw.circle(surface, VERT_FORT, (CENTRE_X, CENTRE_Y), 2)


# ╔══════════════════════════════════════════════════════════╗
# ║                  SOURCES DE DONNÉES                     ║
# ╚══════════════════════════════════════════════════════════╝

def lire_arduino(ser) -> tuple | None:
    if ser and ser.in_waiting:
        try:
            ligne = ser.readline().decode("utf-8").strip()
            a, d  = ligne.split(",")
            return float(a), min(1.0, float(d) / DISTANCE_MAX_CM)
        except Exception:
            pass
    return None


def generer_demo(angle_actuel: float) -> tuple | None:
    """3 zones d'obstacles fixes avec bruit gaussien."""
    zones = [
        (45,  20, 0.55),
        (160, 15, 0.35),
        (290, 25, 0.72),
    ]
    for centre, largeur, dist in zones:
        diff = abs((angle_actuel - centre + 180) % 360 - 180)
        if diff < largeur / 2 and random.random() < 0.25:
            bruit = random.uniform(-0.04, 0.04)
            return angle_actuel, max(0.05, min(1.0, dist + bruit))
    return None


# ╔══════════════════════════════════════════════════════════╗
# ║                   PROGRAMME PRINCIPAL                   ║
# ╚══════════════════════════════════════════════════════════╝

def main():
    # ── Arduino ───────────────────────────────────────────
    ser  = None
    mode = MODE
    if mode == "arduino":
        try:
            import serial
            ser = serial.Serial(PORT_SERIE, BAUD_RATE, timeout=0.05)
            print(f"[OK] Arduino connecté sur {PORT_SERIE}")
        except Exception as e:
            print(f"[WARN] Arduino non trouvé ({e}) → mode démo")
            mode = "demo"

    # ── Assistant vocal ───────────────────────────────────
    print("[VOCAL] Démarrage de l'assistant vocal...")
    assistant = VoiceAssistant()
    assistant.start()
    # Le message de démarrage + init prend quelques secondes
    assistant._pret.wait(timeout=12.0)
    if assistant.pret:
        print("[VOCAL] ✔ Assistant vocal opérationnel")
    else:
        print("[VOCAL] ✘ Voix indisponible — radar sans son")

    # ── Pygame ────────────────────────────────────────────
    pygame.init()
    screen = pygame.display.set_mode((LARGEUR, HAUTEUR))
    pygame.display.set_caption(TITRE)
    clock  = pygame.time.Clock()

    font_titre = pygame.font.SysFont("Courier", 20, bold=True)
    font_sm    = pygame.font.SysFont("Courier", 14)

    # ── État ──────────────────────────────────────────────
    points        : list[PointDetecte] = []
    log           = DetectionLog()
    flash         = AlerteFlash()
    angle_actuel  = 0.0
    pause_jusqu_a = 0.0   # timestamp fin de pause (0 = radar tourne)

    print("[RADAR] Démarré — ESC ou fermer la fenêtre pour quitter.")

    # ── Boucle principale ─────────────────────────────────
    running = True
    while running:
        clock.tick(FPS)

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                running = False

        now      = time.time()
        en_pause = now < pause_jusqu_a

        # Rotation seulement hors pause
        if not en_pause:
            angle_actuel = (angle_actuel + VITESSE_DEG) % 360

        # Détection seulement quand le radar tourne
        detection = None
        if not en_pause:
            detection = (lire_arduino(ser) if mode == "arduino"
                         else generer_demo(angle_actuel))

        if detection:
            a, d = detection
            points.append(PointDetecte(a, d))
            log.ajouter(a, d)
            flash.declencher()
            pause_jusqu_a = now + DUREE_PAUSE_S   # stopper le radar
            assistant.annoncer_detection(a, d)    # lancer la voix

        points = [p for p in points if p.vivant()]

        # ── Rendu ─────────────────────────────────────────
        screen.fill(NOIR)
        dessiner_grille(screen, font_sm)
        dessiner_traine(screen, angle_actuel)
        dessiner_ligne(screen, angle_actuel)
        flash.dessiner(screen)
        dessiner_points(screen, points)
        log.dessiner(screen, font_sm)
        dessiner_hud(
            screen, font_titre, font_sm,
            angle_actuel, len(points), clock.get_fps(),
            vocal_ok  = assistant.pret,
            en_pause  = en_pause,
            tps_pause = max(0.0, pause_jusqu_a - now),
        )
        pygame.display.flip()

    # ── Extinction ────────────────────────────────────────
    assistant.stop()
    assistant.join(timeout=6)
    pygame.quit()
    if ser:
        ser.close()
    sys.exit()


if __name__ == "__main__":
    main()
