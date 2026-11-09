"""Word lists and gazetteers for the rule-based enrichers. Keyword lists marked (ARGUS) come from
microservices/enrichment-svc/scoring.py so both systems score the same vocabulary."""
from __future__ import annotations

CONFLICT_TERMS = [  # (ARGUS)
    "war", "attack", "airstrike", "strike", "bombing", "shelling", "clashes", "militant", "insurgent", "offensive",
    "invasion", "ceasefire", "troops", "military operation", "armed forces", "combat", "gunfire", "missile"]
CASUALTY_TERMS = [  # (ARGUS)
    "killed", "dead", "death toll", "casualties", "wounded", "injured", "fatalities", "victims", "dying", "died",
    "hostage", "kidnapped"]
ECONOMIC_TERMS = [  # (ARGUS)
    "power grid", "blackout", "infrastructure", "supply chain", "pipeline", "market crash", "recession", "inflation",
    "stock market", "bankruptcy", "trade deal", "sanctions", "gdp", "unemployment", "repo rate", "growth"]
POPULATION_TERMS = [  # (ARGUS)
    "displaced", "evacuated", "refugees", "affected residents", "homeless", "population", "shelter", "humanitarian",
    "aid workers", "relief effort"]
BREAKING_TERMS = ["breaking", "just in", "developing story", "live updates", "urgent", "alert", "emergency",
                  "immediate", "unfolding"]

# ---------------------------------------------------------------------------------------------- entity typing
HONORIFICS = {"mr", "mrs", "ms", "dr", "shri", "smt", "sri", "prof", "justice", "captain", "capt", "general", "col",
              "chief minister", "minister", "governor", "speaker", "president", "prime minister", "mla", "mp",
              "senator", "sir", "mayor", "commissioner", "coach", "skipper"}
ORG_SUFFIXES = {"assembly", "party", "ministry", "bank", "court", "commission", "committee", "university", "government",
                "congress", "council", "police", "army", "board", "corporation", "ltd", "limited", "authority",
                "association", "department", "federation", "parliament", "sabha", "indians", "kings", "club", "team",
                "office", "secretariat", "agency", "institute", "forum", "union", "front", "samiti", "dal"}
KNOWN_ORGS = {
    "AAP": "Aam Aadmi Party", "BJP": "Bharatiya Janata Party", "INC": "Indian National Congress",
    "RBI": "Reserve Bank of India", "MPC": "Monetary Policy Committee", "SEBI": "Securities and Exchange Board of India",
    "ISRO": "Indian Space Research Organisation", "BCCI": "Board of Control for Cricket in India",
    "IPL": "Indian Premier League", "UN": "United Nations", "NATO": "NATO", "PTI": "Press Trust of India",
    "ANI": "Asian News International", "ED": "Enforcement Directorate", "CBI": "Central Bureau of Investigation",
    "NIA": "National Investigation Agency", "SC": "Supreme Court", "WHO": "World Health Organization",
    "IMF": "International Monetary Fund", "TNN": "Times News Network", "DMK": "Dravida Munnetra Kazhagam",
    "TMC": "Trinamool Congress", "SAD": "Shiromani Akali Dal", "NCP": "Nationalist Congress Party"}
ORG_ACRONYMS = set(KNOWN_ORGS)
PARTY_NAMES = {"aam aadmi party", "bharatiya janata party", "indian national congress", "trinamool congress",
               "dravida munnetra kazhagam", "shiromani akali dal", "nationalist congress party", "aap", "bjp", "inc",
               "dmk", "tmc", "sad", "ncp", "congress"}
STOP_CAPS = {"the", "a", "an", "in", "on", "at", "of", "and", "but", "he", "she", "it", "they", "his", "her", "this",
             "that", "these", "those", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
             "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
             "november", "december", "nov", "oct", "after", "said", "analysts", "observers", "officials", "residents",
             "doctors", "schools", "markets", "by", "for", "with", "as", "from", "party", "opposition", "growth",
             "bank", "observers", "its", "their", "also", "some", "party", "page", "continued", "contd", "rs", "per",
             "state", "city", "staff", "reporter", "desk", "bureau", "news", "service", "express", "times", "hindu",
             "hindustan", "tnn", "early", "next", "last", "first", "second", "third", "new", "former", "senior"}
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                "ten": 10, "eleven": 11, "twelve": 12, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
                "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}
GROUP_NOUNS = {"mlas": "MLAs", "mla": "MLAs", "legislators": "legislators", "ministers": "ministers", "mps": "MPs",
               "people": "people", "workers": "workers", "farmers": "farmers", "students": "students",
               "protesters": "protesters", "soldiers": "soldiers", "arrests": "arrests", "deaths": "deaths",
               "schools": "schools", "councillors": "councillors", "leaders": "leaders"}

# ---------------------------------------------------------------------------------------------- events
# (regex, event type, trigger lemma) - first match in the headline wins, then the lead sentences
EVENT_TRIGGERS = [
    (r"\b(resign(?:s|ed|ation|ing)?|quits?|quit|steps? down|walks? out)\b", "Politics.Resignation", "resigns"),
    (r"\b(arrest(?:s|ed)?|detained|held for|nabbed)\b", "Crime.Arrest", "arrested"),
    (r"\b(killed|dies|died|death toll|shot dead|murdered)\b", "Conflict.Fatality", "killed"),
    (r"\b(elected|wins? (?:the )?(?:election|polls?|seat)|sworn in|takes oath|by-?election)\b", "Politics.Election", "elected"),
    (r"\b(repo rate|interest rate|rate cut|rate hike|monetary policy|mpc)\b", "Economy.MonetaryPolicy", "holds rate"),
    (r"\b(approv(?:es|ed|al)|clears?|cleared|nod|sanction(?:s|ed))\b", "Government.Approval", "approves"),
    (r"\b(announc(?:es|ed)|unveil(?:s|ed)|launch(?:es|ed)|inaugurat(?:es|ed))\b", "Government.Announcement", "announces"),
    (r"\b(protest(?:s|ed|ers)?|agitation|strike|bandh|rally)\b", "Social.Protest", "protests"),
    (r"\b(appoint(?:s|ed)?|names?|named|new (?:head )?coach|takes charge)\b", "Organisation.Appointment", "appoints"),
    (r"\b(wins?|won|beats?|defeats?|thrash(?:es|ed)|auction|ipl|match)\b", "Sports.Result", "wins"),
    (r"\b(aqi|air quality|pollution|smog|stubble)\b", "Environment.AirQuality", "deteriorates"),
    (r"\b(earthquake|flood|cyclone|landslide|wildfire|storm|cold wave|heat ?wave)\b", "Disaster.NaturalHazard", "strikes"),
    (r"\b(fire|blast|explosion|collision|crash(?:es|ed)?)\b", "Accident.Incident", "occurs"),
    (r"\b(verdict|ruling|rules?|orders?|stays?|hearing|bail)\b", "Judiciary.Ruling", "rules"),
    (r"\b(inflation|gdp|sensex|nifty|stocks?|rally|slump|budget)\b", "Economy.Market", "moves"),
]
EVENT_SEVERITY = {  # 0-1, input to impact_score
    "Conflict.Fatality": 0.9, "Disaster.NaturalHazard": 0.8, "Accident.Incident": 0.7, "Crime.Arrest": 0.55,
    "Politics.Resignation": 0.55, "Politics.Election": 0.6, "Economy.MonetaryPolicy": 0.6, "Judiciary.Ruling": 0.55,
    "Social.Protest": 0.5, "Environment.AirQuality": 0.55, "Government.Approval": 0.4, "Government.Announcement": 0.4,
    "Economy.Market": 0.45, "Organisation.Appointment": 0.3, "Sports.Result": 0.25, "General.Report": 0.2}
FORECAST_BY_EVENT = {
    "Politics.Resignation": "potential for further defections or a by-election; needs time-series context",
    "Politics.Election": "government formation and cabinet allocation likely to follow",
    "Economy.MonetaryPolicy": "next policy meeting and inflation prints will decide direction of rates",
    "Conflict.Fatality": "risk of escalation or retaliation; watch official statements and casualty updates",
    "Disaster.NaturalHazard": "relief operations and damage assessments likely over the next days",
    "Environment.AirQuality": "restrictions and school/work advisories likely while smoke persists",
    "Social.Protest": "possible escalation or talks; watch police and government response",
    "Crime.Arrest": "court remand, bail hearing and investigation updates likely",
    "Judiciary.Ruling": "appeals or compliance orders may follow",
    "Government.Approval": "tendering and implementation timelines are the next milestones",
    "Government.Announcement": "rollout details and opposition reaction likely",
    "Economy.Market": "market follow-through depends on global cues",
    "Organisation.Appointment": "early signals of strategy change expected",
    "Sports.Result": "next fixture and rankings update",
    "Accident.Incident": "inquiry and compensation announcements likely",
    "General.Report": "needs time-series context"}
SUBTOPIC_BY_EVENT = {
    "Politics.Resignation": "legislator resignation / party defection", "Politics.Election": "elections / by-election",
    "Economy.MonetaryPolicy": "central bank / interest rates", "Environment.AirQuality": "air pollution",
    "Conflict.Fatality": "violence / casualties", "Disaster.NaturalHazard": "natural hazards",
    "Crime.Arrest": "policing / arrests", "Judiciary.Ruling": "courts", "Social.Protest": "protests / unrest",
    "Government.Approval": "government decisions", "Government.Announcement": "government announcements",
    "Economy.Market": "markets / economy", "Organisation.Appointment": "appointments", "Sports.Result": "sport"}

# ---------------------------------------------------------------------------------------------- topics
TOPIC_KEYWORDS = {
    "politics": ["assembly", "mla", "minister", "party", "election", "by-election", "government", "opposition", "parliament",
                 "chief minister", "speaker", "bjp", "congress", "aap", "legislator", "vote", "resign", "poll", "cabinet"],
    "business": ["rbi", "repo rate", "inflation", "gdp", "sensex", "nifty", "stocks", "bank", "market", "economy", "growth",
                 "rupee", "investment", "company", "profit", "budget", "monetary policy", "crore", "per cent"],
    "sports": ["ipl", "cricket", "match", "coach", "captain", "tournament", "auction", "goal", "olympic", "wicket",
               "franchise", "squad", "player", "league"],
    "crime": ["arrest", "murder", "police", "court", "accused", "charge", "bail", "theft", "fraud", "custody", "probe"],
    "environment": ["aqi", "pollution", "air quality", "stubble", "smog", "climate", "emission", "forest", "wildlife",
                    "cold wave", "weather", "temperature", "rainfall"],
    "disaster": ["earthquake", "flood", "cyclone", "landslide", "wildfire", "tsunami", "evacuated", "relief"],
    "health": ["hospital", "disease", "vaccine", "doctors", "health", "virus", "patients", "outbreak"],
    "technology": ["ai", "software", "startup", "internet", "cyber", "app", "chip", "digital", "satellite"],
    "world": ["united nations", "foreign", "summit", "treaty", "border", "embassy", "sanctions"],
    "infrastructure": ["metro", "airport", "highway", "railway", "corridor", "project", "bridge", "construction"],
    "entertainment": ["film", "movie", "actor", "actress", "box office", "album", "festival", "bollywood"]}
TOPIC_TO_DOMAIN = {  # ARGUS domain codes: P politics, M military, E economy, S society, Env environment
    "politics": ("politics", "P"), "business": ("economy", "E"), "sports": ("society", "S"), "crime": ("society", "S"),
    "environment": ("environment", "Env"), "disaster": ("environment", "Env"), "health": ("society", "S"),
    "technology": ("economy", "E"), "world": ("politics", "P"), "infrastructure": ("economy", "E"),
    "entertainment": ("society", "S"), "misc": ("misc", "misc")}

# ---------------------------------------------------------------------------------------------- sentiment & tone
NEG_WORDS = {"loss", "loses", "lost", "defection", "defections", "desert", "deserts", "pressure", "crisis", "slump", "killed", "dead", "death", "injured", "attack", "violence", "protest",
             "severe", "dangerous", "poor", "hazardous", "decline", "fall", "falls", "slips", "criticised", "demanded",
             "differences", "banned", "fear", "worst", "collapse", "scam", "fraud", "arrested", "threat", "risks", "risk",
             "elevated", "stubble", "smoke", "breathing"}
POS_WORDS = {"win", "wins", "won", "gain", "gains", "rally", "rallied", "growth", "approved", "approves", "welcomed", "boost",
             "improve", "improves", "success", "record", "relief", "celebrate", "victory", "launch", "inaugurated", "steady",
             "stable", "recovery", "peace", "agreement", "appointed"}
EMOTION_WORDS = {
    "anger": {"angry", "outrage", "slammed", "condemned", "furious", "criticised", "demanded", "blamed", "accused"},
    "fear": {"fear", "threat", "risk", "risks", "danger", "dangerous", "severe", "panic", "alarm", "hazardous", "breathing"},
    "sadness": {"died", "dead", "killed", "mourn", "loss", "tragedy", "death", "grief", "lost"},
    "joy": {"celebrate", "win", "won", "victory", "welcomed", "delighted", "success", "record"},
    "surprise": {"shock", "shocking", "unexpected", "surprise", "sudden", "stunned", "abrupt"}}
CLICKBAIT_PATTERNS = [r"you won'?t believe", r"\bshocking\b", r"\bwhat happened next\b", r"\bthis is why\b",
                      r"\bgoes viral\b", r"\bmust[- ]see\b", r"\bsecret\b", r"\bbreaks the internet\b", r"\bjaw-?dropping\b"]
PROPAGANDA_TERMS = ["traitor", "enemy of the people", "puppet", "regime", "terrorist sympathiser", "anti-national",
                    "fake news media", "mastermind of lies", "destroy our nation", "foreign hand", "hate mongers"]

# ---------------------------------------------------------------------------------------------- geography
# name -> (lat, lon, level, parent state/country). Centroids are approximate.
INDIA_STATES = {
    "punjab": (31.15, 75.34), "haryana": (29.06, 76.09), "delhi": (28.61, 77.21), "maharashtra": (19.75, 75.71),
    "gujarat": (22.26, 71.19), "rajasthan": (27.02, 74.22), "uttar pradesh": (26.85, 80.91), "bihar": (25.10, 85.31),
    "west bengal": (22.99, 87.85), "tamil nadu": (11.13, 78.66), "kerala": (10.85, 76.27), "karnataka": (15.32, 75.71),
    "andhra pradesh": (15.91, 79.74), "telangana": (18.11, 79.02), "madhya pradesh": (22.97, 78.66),
    "odisha": (20.95, 85.10), "assam": (26.20, 92.94), "jharkhand": (23.61, 85.28), "chhattisgarh": (21.28, 81.87),
    "uttarakhand": (30.07, 79.02), "himachal pradesh": (31.10, 77.17), "jammu and kashmir": (33.78, 76.58),
    "goa": (15.30, 74.12), "manipur": (24.66, 93.91), "meghalaya": (25.47, 91.37), "tripura": (23.94, 91.99),
    "chandigarh": (30.73, 76.78), "ladakh": (34.15, 77.58)}
INDIA_CITIES = {  # city -> (lat, lon, state)
    "new delhi": (28.61, 77.21, "delhi"), "mumbai": (19.08, 72.88, "maharashtra"), "kolkata": (22.57, 88.36, "west bengal"),
    "chennai": (13.08, 80.27, "tamil nadu"), "bengaluru": (12.97, 77.59, "karnataka"), "bangalore": (12.97, 77.59, "karnataka"),
    "hyderabad": (17.39, 78.49, "telangana"), "pune": (18.52, 73.86, "maharashtra"), "ahmedabad": (23.02, 72.57, "gujarat"),
    "lucknow": (26.85, 80.95, "uttar pradesh"), "jaipur": (26.91, 75.79, "rajasthan"), "patna": (25.59, 85.14, "bihar"),
    "bhopal": (23.26, 77.41, "madhya pradesh"), "ludhiana": (30.90, 75.86, "punjab"), "amritsar": (31.63, 74.87, "punjab"),
    "jalandhar": (31.33, 75.58, "punjab"), "kochi": (9.93, 76.27, "kerala"), "thiruvananthapuram": (8.52, 76.94, "kerala"),
    "guwahati": (26.14, 91.74, "assam"), "srinagar": (34.08, 74.80, "jammu and kashmir"), "dehradun": (30.32, 78.03, "uttarakhand"),
    "shimla": (31.10, 77.17, "himachal pradesh"), "ranchi": (23.34, 85.31, "jharkhand"), "raipur": (21.25, 81.63, "chhattisgarh"),
    "bhubaneswar": (20.30, 85.82, "odisha"), "nagpur": (21.15, 79.09, "maharashtra"), "surat": (21.17, 72.83, "gujarat"),
    "indore": (22.72, 75.86, "madhya pradesh"), "chandigarh": (30.73, 76.78, "chandigarh"), "gurugram": (28.46, 77.03, "haryana"),
    "noida": (28.54, 77.39, "uttar pradesh"), "delhi": (28.61, 77.21, "delhi")}
COUNTRY_CENTROIDS = {"IN": (22.0, 79.0), "PK": (30.4, 69.3), "CN": (35.9, 104.2), "US": (39.8, -98.6), "GB": (54.0, -2.0),
                     "RU": (61.5, 105.3), "UA": (48.4, 31.2), "IL": (31.0, 35.0), "BD": (23.7, 90.4), "NP": (28.4, 84.1),
                     "LK": (7.9, 80.8), "AF": (33.9, 67.7), "FR": (46.2, 2.2), "DE": (51.2, 10.5), "JP": (36.2, 138.3)}
# names that exist in India and elsewhere: name -> (other place, cue words that mean India)
AMBIGUOUS_PLACES = {"punjab": ("Pakistan", {"aap", "chandigarh", "india", "rs.", "lok sabha", "assembly", "mla", "bjp",
                                            "congress", "ludhiana", "amritsar", "crore"}),
                    "hyderabad": ("Pakistan (Sindh)", {"telangana", "india", "rs.", "crore"}),
                    "bengal": ("Bangladesh", {"west bengal", "kolkata", "india", "rs."})}
