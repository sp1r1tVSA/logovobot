"""
services/player_positions.py

Intelligent Player Position Resolution Engine for EA FC / FUT Cards & Club Squads.
Determines authentic football positions (ST, LW, RW, CAM, CM, CDM, CB, LB, RB, GK, etc.)
via multi-layer resolution:
1. Canonical FIFA / EA FC position registry (200+ known stars and tournament players)
2. Normalized multi-lingual position mappings (Russian, English, Spanish, Portuguese)
3. Online FotMob & TheSportsDB metadata extraction with offline fallbacks
"""

import re
import json
import logging
import urllib.parse
import urllib.request
import unicodedata

logger = logging.getLogger(__name__)

# Standard EA FC Positions
VALID_POSITIONS = {
    "GK", "CB", "LB", "RB", "LWB", "RWB",
    "CDM", "CM", "CAM", "LM", "RM",
    "LW", "RW", "CF", "ST"
}

# Normalization mapping for Russian, English and regional abbreviations
POSITION_ALIASES: dict[str, str] = {
    # Goalkeepers
    "gk": "GK", "goalkeeper": "GK", "вратарь": "GK", "врт": "GK", "воротник": "GK", "portero": "GK", "goleiro": "GK",
    
    # Defenders
    "cb": "CB", "centre-back": "CB", "center back": "CB", "central defender": "CB", "цз": "CB", "центральный защитник": "CB", "защитник": "CB", "def": "CB",
    "lb": "LB", "left-back": "LB", "left back": "LB", "лз": "LB", "левый защитник": "LB", "lateral izquierdo": "LB",
    "rb": "RB", "right-back": "RB", "right back": "RB", "пз": "RB", "правый защитник": "RB", "lateral derecho": "RB",
    "lwb": "LWB", "left wing back": "LWB", "ллз": "LWB", "латераль": "LWB",
    "rwb": "RWB", "right wing back": "RWB", "плз": "RWB",
    
    # Midfielders
    "cdm": "CDM", "defensive midfield": "CDM", "defensive midfielder": "CDM", "цоп": "CDM", "опорник": "CDM", "опорный полузащитник": "CDM", "pivote": "CDM", "volante": "CDM",
    "cm": "CM", "central midfield": "CM", "central midfielder": "CM", "цп": "CM", "центральный полузащитник": "CM", "полузащитник": "CM", "mid": "CM", "midfielder": "CM",
    "cam": "CAM", "attacking midfield": "CAM", "attacking midfielder": "CAM", "цап": "CAM", "плеймейкер": "CAM", "атакующий полузащитник": "CAM", "mediapunta": "CAM",
    "lm": "LM", "left midfield": "LM", "left midfielder": "LM", "лп": "LM", "левый полузащитник": "LM",
    "rm": "RM", "right midfield": "RM", "right midfielder": "RM", "пп": "RM", "правый полузащитник": "RM",
    
    # Attackers / Wingers
    "lw": "LW", "left wing": "LW", "left winger": "LW", "лв": "LW", "левый вингер": "LW", "левый нападающий": "LW", "extremo izquierdo": "LW", "ponta esquerda": "LW",
    "rw": "RW", "right wing": "RW", "right winger": "RW", "пв": "RW", "правый вингер": "RW", "правый нападающий": "RW", "extremo derecho": "RW", "ponta direita": "LW",
    "cf": "CF", "centre-forward": "CF", "second striker": "CF", "оттянутый нападающий": "CF", "под нападающим": "CF",
    "st": "ST", "striker": "ST", "нап": "ST", "нападающий": "ST", "форвард": "ST", "центрфорвард": "ST", "delantero": "ST", "centroavante": "ST", "att": "ST", "forward": "ST"
}

# ═════════════════════════════════════════════════════════════════════════════
# ⚽ CANONICAL REAL-WORLD PLAYER POSITION REGISTRY (200+ PLAYERS)
# ═════════════════════════════════════════════════════════════════════════════
KNOWN_PLAYER_POSITIONS: dict[str, str] = {
    # ── Auto-synced Club Players from Server DB ──
    "addai": "ST",
    "cristian tello": "CM",
    "dodi lukebakio": "RW",
    "evander": "LW",
    "giacomo raspadori": "ST",
    "gronbaek": "CM",
    "grønbæk": "CM",
    "jamie bynoe-gittens": "CM",
    "joao pedro": "ST",
    "noa lang": "LW",
    "orbelin pineda": "CM",
    "ricardo horta": "ST",
    "seko fofana": "CM",
    "vinicius jr реал": "LW",

    # Strikers / Forwards (ST / CF)
    "erling haaland": "ST", "haaland": "ST", "эрлинг холанд": "ST", "холанд": "ST",
    "kylian mbappe": "ST", "mbappe": "ST", "килиан мбаппе": "ST", "мбаппе": "ST",
    "harry kane": "ST", "kane": "ST", "гарри кейн": "ST", "кейн": "ST",
    "viktor gyokeres": "ST", "gyokeres": "ST", "виктор дьёкереш": "ST", "дьёкереш": "ST", "дьекереш": "ST",
    "robert lewandowski": "ST", "lewandowski": "ST", "роберт левандовски": "ST", "левандовски": "ST",
    "lautaro martinez": "ST", "lautaro": "ST", "лаутаро мартинес": "ST", "лаутаро": "ST",
    "victor osimhen": "ST", "osimhen": "ST", "виктор осимхен": "ST", "осимхен": "ST",
    "alexander isak": "ST", "isak": "ST", "александр исак": "ST", "исак": "ST",
    "ollie watkins": "ST", "watkins": "ST", "олли уоткинс": "ST",
    "darwin nunez": "ST", "nunez": "ST", "дарвин нуньес": "ST", "нуньес": "ST",
    "kai havertz": "ST", "havertz": "ST", "кай хавертц": "ST", "хавертц": "ST",
    "nicolas jackson": "ST", "jackson": "ST", "николас джексон": "ST",
    "santiago gimenez": "ST", "gimenez": "ST", "санти хименес": "ST", "хименес": "ST",
    "alvaro morata": "ST", "morata": "ST", "альваро мората": "ST", "мората": "ST",
    "dusan vlahovic": "ST", "vlahovic": "ST", "душан влахович": "ST", "влахович": "ST",
    "marcus thuram": "ST", "thuram": "ST", "маркус тюрам": "ST", "тюрам": "ST",
    "rasmus hojlund": "ST", "hojlund": "ST", "расмус хойлунд": "ST", "хойлунд": "ST",
    "julian alvarez": "ST", "alvarez": "ST", "хулиан альварес": "ST", "альварес": "ST",
    "artem dovbyk": "ST", "dovbyk": "ST", "артем довбик": "ST", "довбик": "ST",
    "serhou guirassy": "ST", "guirassy": "ST", "серу гирасси": "ST", "гирасси": "ST",
    "mateo retegui": "ST", "retegui": "ST", "матео ретеги": "ST",
    "jonathan david": "ST", "david": "ST", "джонатан дэвид": "ST",
    "benjamin sesko": "ST", "sesko": "ST", "беньямин шешко": "ST", "шешко": "ST",
    "samu omorodion": "ST", "omorodion": "ST", "саму омородион": "ST",

    # Left Wingers / Attackers (LW / LM)
    "vinicius jr": "LW", "vinicius junior": "LW", "vini jr": "LW", "винисиус": "LW", "винисиус жуниор": "LW",
    "rafael leao": "LW", "leao": "LW", "рафаэль леау": "LW", "леау": "LW",
    "khvicha kvaratskhelia": "LW", "kvaratskhelia": "LW", "хвича кварацхелия": "LW", "хвича": "LW",
    "luis diaz": "LW", "diaz": "LW", "луис диас": "LW", "диас": "LW",
    "gabriel martinelli": "LW", "martinelli": "LW", "габриэл мартинелли": "LW", "мартинелли": "LW",
    "son heung-min": "LW", "son": "LW", "сон хын мин": "LW", "сон": "LW",
    "nico williams": "LW", "williams": "LW", "нико уильямс": "LW",
    "jeremy doku": "LW", "doku": "LW", "жереми доку": "LW", "доку": "LW",
    "bradley barcola": "LW", "barcola": "LW", "брэдли барколя": "LW", "барколя": "LW",
    "galeno": "LW", "галено": "LW", "wenderson galeno": "LW",
    "artur gomes": "LW", "артур гомес": "LW", "artur": "LW",
    "jack grealish": "LW", "grealish": "LW", "джек грилиш": "LW", "грилиш": "LW",
    "cody gakpo": "LW", "gakpo": "LW", "коди гакпо": "LW", "гакпо": "LW",
    "marcus rashford": "LW", "rashford": "LW", "маркус рэшфорд": "LW", "рэшфорд": "LW",
    "jamie gittens": "LW", "джейми гиттенс": "LW", "binoe-gittens": "LW",

    # Right Wingers / Attackers (RW / RM)
    "mohamed salah": "RW", "salah": "RW", "мохамед салах": "RW", "салах": "RW",
    "bukayo saka": "RW", "saka": "RW", "букайо сака": "RW", "сака": "RW",
    "lamine yamal": "RW", "yamal": "RW", "ламин ямаль": "RW", "ямаль": "RW",
    "rodrygo": "RW", "родриго": "RW", "rodrygo goes": "RW",
    "michael olise": "RW", "olise": "RW", "майкл олисе": "RW", "олисе": "RW",
    "leroy sane": "RW", "sane": "RW", "лерой сане": "RW", "сане": "RW",
    "ousmane dembele": "RW", "dembele": "RW", "усман дембеле": "RW", "дембеле": "RW",
    "roony bardghji": "RW", "bardghji": "RW", "руни бардгжи": "RW", "бардгжи": "RW",
    "igor paixao": "RW", "paixao": "RW", "игор пайшао": "RW", "пайшао": "RW",
    "david neres": "RW", "neres": "RW", "давид нерес": "RW", "нерес": "RW",
    "takefusa kubo": "RW", "kubo": "RW", "такефуса кубо": "RW", "кубо": "RW",
    "savinho": "RW", "савиньо": "RW", "savio": "RW",
    "raphinha": "RW", "рафинья": "RW",
    "antony": "RW", "антони": "RW",
    "pedro neto": "RW", "neto": "RW", "педру нету": "RW",

    # Attacking Midfielders / Playmakers (CAM / AM)
    "ryan christie": "CAM", "christie": "CAM", "райан кристи": "CAM", "кристи": "CAM",
    "matias zaracho": "CAM", "zaracho": "CAM", "матияс сарачо": "CAM", "сарачо": "CAM",
    "jude bellingham": "CAM", "bellingham": "CAM", "джуд беллингем": "CAM", "беллингем": "CAM",
    "florian wirtz": "CAM", "wirtz": "CAM", "флориан вирц": "CAM", "вирц": "CAM",
    "jamal musiala": "CAM", "musiala": "CAM", "джамал мусиала": "CAM", "мусиала": "CAM",
    "martin odegaard": "CAM", "odegaard": "CAM", "мартин эдегор": "CAM", "эдегор": "CAM",
    "cole palmer": "CAM", "palmer": "CAM", "коул палмер": "CAM", "палмер": "CAM",
    "bruno fernandes": "CAM", "бруну фернандеш": "CAM", "фернандеш": "CAM",
    "kevin de bruyne": "CAM", "de bruyne": "CAM", "кевин де брюйне": "CAM", "де брюйне": "CAM",
    "bernardo silva": "CAM", "бернарду силва": "CAM",
    "dominik szoboszlai": "CAM", "szoboszlai": "CAM", "доминик собослаи": "CAM", "собослаи": "CAM",
    "dani olmo": "CAM", "olmo": "CAM", "дани ольмо": "CAM", "ольмо": "CAM",
    "james maddison": "CAM", "maddison": "CAM", "джеймс мэддисон": "CAM",
    "lucas paquetá": "CAM", "paqueta": "CAM", "лукас пакета": "CAM",
    "arda guler": "CAM", "guler": "CAM", "арда гюлер": "CAM", "гюлер": "CAM",
    "brahim diaz": "CAM", "браим диас": "CAM",
    "xavi simons": "CAM", "simons": "CAM", "хави симонс": "CAM",

    # Central Midfielders (CM)
    "pedri": "CM", "педри": "CM",
    "gavi": "CM", "гави": "CM",
    "federico valverde": "CM", "valverde": "CM", "федерико вальверде": "CM", "вальверде": "CM",
    "eduardo camavinga": "CM", "camavinga": "CM", "эдуардо камавинга": "CM", "камавинга": "CM",
    "nicolo barella": "CM", "barella": "CM", "николо барелла": "CM", "барелла": "CM",
    "vitinha": "CM", "витинья": "CM",
    "alexis mac allister": "CM", "mac allister": "CM", "алексис макаллистер": "CM",
    "frenkie de jong": "CM", "de jong": "CM", "френки де йонг": "CM", "де йонг": "CM",
    "luka modric": "CM", "modric": "CM", "лука модрич": "CM", "модрич": "CM",
    "toni kroos": "CM", "kroos": "CM", "тони кроос": "CM", "кроос": "CM",
    "tijjani reijnders": "CM", "reijnders": "CM", "тиджани рейндерс": "CM",
    "warren zaire-emery": "CM", "zaire-emery": "CM", "варрен заир-эмери": "CM",
    "kobbie mainoo": "CM", "mainoo": "CM", "кобби майну": "CM", "майну": "CM",
    "joao neves": "CM", "neves": "CM", "жоао невеш": "CM",

    # Defensive Midfielders (CDM)
    "rodri": "CDM", "родри": "CDM",
    "declan rice": "CDM", "rice": "CDM", "деклин райс": "CDM", "райс": "CDM",
    "aurelien tchouameni": "CDM", "tchouameni": "CDM", "орельен чуамени": "CDM", "чуамени": "CDM",
    "joshua kimmich": "CDM", "kimmich": "CDM", "йозуа киммих": "CDM", "киммих": "CDM",
    "moises caicedo": "CDM", "caicedo": "CDM", "мозес кайседо": "CDM", "кайседо": "CDM",
    "casemiro": "CDM", "каземиро": "CDM",
    "bruno guimaraes": "CDM", "guimaraes": "CDM", "бруно гимараэс": "CDM",
    "joao palhinha": "CDM", "palhinha": "CDM", "жоао пальинья": "CDM",
    "granit xhaka": "CDM", "xhaka": "CDM", "гранит джака": "CDM",
    "manuel ugarte": "CDM", "ugarte": "CDM", "мануэль угарте": "CDM",

    # Centre-Backs (CB)
    "virgil van dijk": "CB", "van dijk": "CB", "вирджил ван дейк": "CB", "ван дейк": "CB",
    "william saliba": "CB", "saliba": "CB", "вильям салиба": "CB", "салиба": "CB",
    "gabriel magalhaes": "CB", "gabriel": "CB", "габриэл магальяэс": "CB",
    "antonio rudiger": "CB", "rudiger": "CB", "антонио рюдигер": "CB", "рюдигер": "CB",
    "ruben dias": "CB", "рубен диаш": "CB", "диаш": "CB",
    "alessandro bastoni": "CB", "bastoni": "CB", "алессандро бастони": "CB", "бастони": "CB",
    "eder militao": "CB", "militao": "CB", "эдер милитао": "CB", "милитао": "CB",
    "ronald araujo": "CB", "araujo": "CB", "рональд араухо": "CB", "араухо": "CB",
    "pau cubarsi": "CB", "cubarsi": "CB", "пау кубарси": "CB", "кубарси": "CB",
    "micky van de ven": "CB", "van de ven": "CB", "микки ван де вен": "CB",
    "cristian romero": "CB", "romero": "CB", "кристиан ромеро": "CB", "ромеро": "CB",
    "john stones": "CB", "stones": "CB", "джон стоунз": "CB",
    "marquinhos": "CB", "маркиньос": "CB",
    "gleison bremer": "CB", "bremer": "CB", "глесон бремер": "CB", "бремер": "CB",
    "lisandro martinez": "CB", "лисандро мартинес": "CB",
    "dayot upamecano": "CB", "upamecano": "CB", "дайо упамекано": "CB",
    "kim min-jae": "CB", "ким мин чжэ": "CB",
    "jonathan tah": "CB", "tah": "CB", "джонатан та": "CB",

    # Full-Backs (LB / RB / LWB / RWB)
    "gerard martin": "LB", "gerard martín": "LB", "жерар мартин": "LB",
    "alphonso davies": "LB", "davies": "LB", "альфонсо дэвис": "LB", "дэвис": "LB",
    "theo hernandez": "LB", "hernandez": "LB", "тео эрнандес": "LB", "эрнандес": "LB",
    "alejandro grimaldo": "LB", "grimaldo": "LB", "алехандро гримальдо": "LB", "гримальдо": "LB",
    "francisco moura": "LB", "moura": "LB", "франсишку моура": "LB", "моура": "LB",
    "nuno mendes": "LB", "нуну мендеш": "LB", "мендеш": "LB",
    "destiny udogie": "LB", "udogie": "LB", "дестини удоджи": "LB",
    "josko gvardiol": "LB", "gvardiol": "LB", "йошко гвардиол": "LB", "гвардиол": "LB",
    "andrew robertson": "LB", "robertson": "LB", "эндрю робертсон": "LB",
    "ferland mendy": "LB", "ферлан менди": "LB",
    "alejandro balde": "LB", "balde": "LB", "алехандро бальде": "LB", "бальде": "LB",

    "dani carvajal": "RB", "carvajal": "RB", "дани карвахаль": "RB", "карвахаль": "RB",
    "trent alexander-arnold": "RB", "alexander-arnold": "RB", "трент александер-арнольд": "RB", "трент": "RB",
    "jeremie frimpong": "RWB", "frimpong": "RWB", "жереми фримпонг": "RWB", "фримпонг": "RWB",
    "achraf hakimi": "RB", "hakimi": "RB", "ашраф хакими": "RB", "хакими": "RB",
    "kyle walker": "RB", "walker": "RB", "кайл уокер": "RB", "уокер": "RB",
    "ben white": "RB", "бен уайт": "RB",
    "jules kounde": "RB", "kounde": "RB", "жюль кунде": "RB", "кунде": "RB",
    "pedro porro": "RB", "porro": "RB", "педро порро": "RB",
    "federico dimarco": "LWB", "dimarco": "LWB", "федерико димарко": "LWB", "димарко": "LWB",

    # Goalkeepers (GK)
    "thibaut courtois": "GK", "courtois": "GK", "тибо куртуа": "GK", "куртуа": "GK",
    "alisson becker": "GK", "alisson": "GK", "алиссон": "GK",
    "ederson": "GK", "эдерсон": "GK",
    "jan oblak": "GK", "oblak": "GK", "ян облак": "GK", "облак": "GK",
    "mike maignan": "GK", "maignan": "GK", "майк меньян": "GK", "меньян": "GK",
    "marc-andre ter stegen": "GK", "ter stegen": "GK", "тер штеген": "GK",
    "david raya": "GK", "raya": "GK", "давид райя": "GK", "райя": "GK",
    "gianluigi donnarumma": "GK", "donnarumma": "GK", "джиджи доннарумма": "GK", "доннарумма": "GK",
    "emiliano martinez": "GK", "dibu martinez": "GK", "эмилиано мартинес": "GK", "дибу": "GK",
    "gregor kobel": "GK", "kobel": "GK", "грегор кобель": "GK", "кобель": "GK",
    "unai simon": "GK", "унай симон": "GK",
    "diogo costa": "GK", "диогу кошта": "GK", "кошта": "GK",
    "andrash schezny": "GK", "wojciech szczesny": "GK", "щесны": "GK",
}


def normalize_position(pos_str: str | None) -> str:
    """Normalize any position string (Russian/English/abbreviation) into canonical EA FC code."""
    if not pos_str:
        return "ST"
    
    clean = str(pos_str).strip().lower()
    clean = re.sub(r"[^\w\s-]", "", clean)
    
    # Direct match in aliases
    if clean in POSITION_ALIASES:
        return POSITION_ALIASES[clean]
    
    # Exact uppercase match
    upper = str(pos_str).strip().upper()
    if upper in VALID_POSITIONS:
        return upper
    
    # Check words
    for word in clean.split():
        if word in POSITION_ALIASES:
            return POSITION_ALIASES[word]
            
    return "ST"


def _normalize_name_key(name: str) -> str:
    clean = unicodedata.normalize("NFKD", name)
    clean = "".join([c for c in clean if not unicodedata.combining(c)])
    clean = clean.lower().strip()
    clean = re.sub(r"[^\w\s]", "", clean)
    return re.sub(r"\s+", " ", clean)


def known_position(player_name: str | None) -> str | None:
    """Position from the built-in registry only — never goes online.

    The same two registry steps `detect_player_position` starts with (exact key,
    then a long-enough token), for callers such as the TOTW job that must not
    block on HTTP. None when the registry does not know the player.
    """
    if not player_name:
        return None
    p_norm = _normalize_name_key(player_name)
    if p_norm in KNOWN_PLAYER_POSITIONS:
        return KNOWN_PLAYER_POSITIONS[p_norm]
    for k, v in KNOWN_PLAYER_POSITIONS.items():
        if len(k) >= 5 and k in p_norm:
            return v
    return None


def _fetch_thesportsdb_position(player_name: str) -> str | None:
    """Query TheSportsDB API to extract player's real world position."""
    try:
        query = urllib.parse.quote(player_name)
        url = f"https://www.thesportsdb.com/api/v1/json/3/searchplayers.php?p={query}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            players = data.get("player")
            if players and len(players) > 0:
                pos = players[0].get("strPosition")
                if pos:
                    return normalize_position(pos)
    except Exception as e:
        logger.debug(f"TheSportsDB position search failed for '{player_name}': {e}")
    return None


def _fetch_wikipedia_position(player_name: str) -> str | None:
    """Extract player position from Wikipedia lead sentence / description."""
    try:
        clean = player_name.replace(" ", "_")
        candidates = [clean, f"{clean}_(footballer)", f"{clean}_(soccer)"]
        for c in candidates:
            url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(c)}"
            req = urllib.request.Request(url, headers={"User-Agent": "Logovobot/1.0 (contact@logovo.bot)"})
            try:
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    text = (data.get("description", "") + " " + data.get("extract", "")).lower()
                    if not text:
                        continue
                    if "attacking midfielder" in text:
                        return "CAM"
                    if "defensive midfielder" in text:
                        return "CDM"
                    if "central midfielder" in text or "center midfielder" in text:
                        return "CM"
                    if "left winger" in text or "left wing" in text:
                        return "LW"
                    if "right winger" in text or "right wing" in text:
                        return "RW"
                    if "winger" in text:
                        return "RW"
                    if "centre-back" in text or "central defender" in text:
                        return "CB"
                    if "left-back" in text or "left back" in text:
                        return "LB"
                    if "right-back" in text or "right back" in text:
                        return "RB"
                    if "goalkeeper" in text:
                        return "GK"
                    if "striker" in text or "centre-forward" in text or "forward" in text:
                        return "ST"
                    if "midfielder" in text:
                        return "CM"
                    if "defender" in text:
                        return "CB"
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"Wikipedia position search failed for '{player_name}': {e}")
    return None


def _fetch_fotmob_position(player_name: str) -> str | None:
    """Query FotMob Search API to extract player's real world position."""
    try:
        query = urllib.parse.quote(player_name)
        url = f"https://apigw.fotmob.com/searchapi/suggest?term={query}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json"
            }
        )
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            squad_members = data.get("squadMemberSuggest", [])
            if squad_members:
                first = squad_members[0]
                role = first.get("role") or first.get("position")
                if role:
                    return normalize_position(str(role))
    except Exception as e:
        logger.debug(f"FotMob position search failed for '{player_name}': {e}")
    return None


def detect_player_position(player_name: str, team_name: str | None = None, fallback_goals: int = 0, fallback_assists: int = 0) -> str:
    """
    Resolve the authentic football position for a player.
    1. Built-in Known Positions Registry (0ms)
    2. Online TheSportsDB API
    3. Online Wikipedia REST API
    4. Online FotMob API
    5. Heuristic fallback based on goals / assists
    """
    if not player_name:
        return "ST"
        
    p_norm = _normalize_name_key(player_name)
    
    # 1. Exact match in canonical registry
    if p_norm in KNOWN_PLAYER_POSITIONS:
        return KNOWN_PLAYER_POSITIONS[p_norm]
        
    # 2. Token match (e.g. "Vinicius" or "Gyokeres" in full name)
    for k, v in KNOWN_PLAYER_POSITIONS.items():
        if len(k) > 3 and (k == p_norm or (k in p_norm and len(k) >= 5)):
            return v
            
    # 3. Online metadata discovery (TheSportsDB -> Wikipedia -> FotMob)
    try:
        tsdb_pos = _fetch_thesportsdb_position(player_name)
        if tsdb_pos:
            return tsdb_pos
    except Exception:
        pass

    try:
        wiki_pos = _fetch_wikipedia_position(player_name)
        if wiki_pos:
            return wiki_pos
    except Exception:
        pass

    try:
        fotmob_pos = _fetch_fotmob_position(player_name)
        if fotmob_pos:
            return fotmob_pos
    except Exception:
        pass
        
    # 4. Statistical heuristic fallback
    if fallback_assists > fallback_goals and fallback_assists >= 3:
        return "CAM"
    return "ST"
