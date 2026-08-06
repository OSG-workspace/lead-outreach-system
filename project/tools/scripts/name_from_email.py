#!/usr/bin/env python3
"""Derive a decision-maker's NAME from an email address — deterministically.

WHY THIS EXISTS (user directive, re-affirmed 2026-07-31)
Stage 5.5 used to dispatch one `name-finder` Haiku per qualified lead, always.
Measured on 2026-07-31-au-trades: 179 agents ran, 7 leads survived — 96% of the
fan-out produced nothing. And measured on the leads that DID survive, the name
is already sitting in the email local-part 85% of the time
(2026-07-31-eu-hotels 41/48, 2026-07-30-eu-hotels 42/57, au-trades 6/7).

So the name does not need to be searched for when the address already carries
it: `neil.baker@bakertradeservices.com.au` is Neil Baker without a single
tool call. This module does that parse. Stage 5.5 runs it FIRST and only
dispatches an agent for the leads it could not resolve — same output contract,
strictly fewer agents, zero tokens for the ones it answers.

WHY CORROBORATION IS NOT OPTIONAL
Local parts look like names far more often than they are one. Probed against the
real dropped-lead sets, a bare "single token that isn't a role word" accepts
`budapest@examplehotel.hu`, `recepce@hotelexample.com`, `fom@examplehotel.hu`
(= front office manager), `foleon@collinahotels.com`, `minerva@minerva-example.ro`.
Each of those would put "Hello Budapest," at the top of a cold email.

So a single-token local part is accepted ONLY when the token also appears as a
standalone capitalised word in the business's OWN already-scraped pages — free
evidence, already on disk from Stage 4, that a person by that name is connected
to the business. A dotted `first.last` is accepted without page evidence
(the pattern itself is the evidence) but still has to clear the role/brand nets.

Everything unproven is left to the agent, never guessed. Same principle as
resolve_domains.py: a wrong name is worse than a missing one.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from email_utils import ROLE_RE, PLACEHOLDER_LOCALS, FREEMAIL, root_domain
except Exception:                                    # standalone / import-order safety
    ROLE_RE = re.compile(r"^(info|contact|hello|sales|admin|support|office)@", re.I)
    PLACEHOLDER_LOCALS = {"name", "example", "test", "user", "email"}
    FREEMAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com"}
    def root_domain(s: str) -> str:                   # noqa: E306
        return s.split("@")[-1].lower()

# Local-part tokens that are a JOB, a DEPARTMENT, a PLACE or a PRODUCT — never a
# person. ROLE_RE already covers the common English mailboxes; this adds the
# abbreviations and non-English forms actually observed in the run logs, plus the
# hospitality/trades vocabulary those two campaigns are full of.
NOT_A_NAME = {
    # role abbreviations seen in eu-hotels drops
    "fom", "fbm", "fb", "gm", "agm", "dosm", "dos", "rm", "res", "resa", "reserv",
    "mgr", "mgmt", "dir", "exec", "asst", "sec", "acc", "adm", "ops", "hk",
    # non-English reception / booking / management mailboxes
    "recepce", "recepcia", "receptie", "recepcion", "recepcao", "rezervari",
    "rezervace", "rezerwacje", "prenotazioni", "prenotazione", "reservas",
    "reservations", "reservierung", "buchung", "empfang", "direzione", "direccion",
    "direction", "ricevimento", "accoglienza", "kontakt", "anfrage", "auskunft",
    "bureau", "oficina", "ufficio", "biuro", "iroda", "kancelar", "sekretariat",
    "vendas", "ventas", "vertrieb", "verkauf", "commerciale", "comercial",
    # generic web / site / form mailboxes
    "site", "website", "web", "webmaster", "webmail", "www", "host", "hosting",
    "mailbox", "postmaster", "hostmaster", "abuse", "root", "daemon",
    "contactus", "contact-us", "getintouch", "enquire", "inquire", "ask", "help",
    "hallo", "bonjour", "ciao", "hola", "ola", "salut", "merhaba", "ahoy",
    # trades / hospitality vocabulary (au-trades + eu-hotels)
    "plumbing", "plumber", "electrical", "electrician", "hvac", "aircon",
    "roofing", "works", "trades", "trade", "emergency", "callout", "service",
    "servicing", "repairs", "quotes", "quote", "estimate", "booking", "bookings",
    "dispatch", "jobs", "job", "maintenance", "install", "installs",
    "hotel", "hotels", "motel", "resort", "spa", "wellness", "restaurant",
    "restaurace", "ristorante", "bistro", "bar", "cafe", "kitchen", "banquet",
    "banqueting", "catering", "events", "event", "wedding", "weddings", "mice",
    "groups", "group", "rooms", "room", "suites", "stay", "guest", "guests",
    "concierge", "porter", "housekeeping", "frontdesk", "frontoffice", "revenue",
    "loyalty", "membership", "voucher", "gift", "shop", "store", "boutique",
    # money / legal / hr
    "invoice", "invoices", "billing", "payments", "payment", "finance", "accounts",
    "accounting", "payroll", "tax", "legal", "compliance", "privacy", "gdpr",
    "dpo", "hr", "recruitment", "careers", "career", "apply", "cv", "resume",
    "training", "academy", "school",
    # misc noise
    "noreply", "no-reply", "donotreply", "notifications", "alerts", "system",
    "mail", "email", "inbox", "news", "newsletter", "press", "media", "pr",
    "marketing", "social", "blog", "feedback", "reviews", "survey", "data",
    "it", "tech", "dev", "api", "test", "demo", "temp", "old", "new", "all",
    "team", "staff", "crew", "people", "everyone", "general", "main", "head",
    # The plain-English role words. ROLE_RE only fires when one of these is the
    # WHOLE local part, so `reception.qld@`, `sales.nsw@`, `admin.team@` slip
    # past it — they have to be blocked as sub-tokens too.
    "info", "contact", "hello", "enquiry", "enquiries", "inquiry", "inquiries",
    "reception", "receptionist", "admin", "administration", "support", "office",
    "sales", "hi", "customercare", "customerservice", "care", "client", "clients",
    "customer", "customers", "orders", "order", "shipping", "returns", "supply",
    "purchasing", "procurement", "tenders", "tender", "partners", "partnership",
    "affiliates", "vendor", "vendors", "supplier", "suppliers",
    # Region / state / branch qualifiers that pair with a role word.
    "qld", "nsw", "vic", "wa", "sa", "nt", "act", "tas", "aus", "au", "uk", "us",
    "usa", "eu", "emea", "apac", "north", "south", "east", "west", "central",
    "national", "local", "regional", "branch", "depot", "hq", "corporate",
    "sydney", "melbourne", "brisbane", "perth", "adelaide", "canberra", "hobart",
    "darwin", "cairns", "townsville", "geelong", "newcastle", "wollongong",
}

# Country/ISP freemail variants missing from the base FREEMAIL set. A freemail
# address is a real person's mailbox but tells us nothing about the BUSINESS, and
# the email gate rejects it — so deriving a name from one is wasted work.
FREEMAIL_EXTRA = {
    "live.com.au", "hotmail.com.au", "yahoo.com.au", "outlook.com.au",
    "bigpond.com", "bigpond.net.au", "optusnet.com.au", "iinet.net.au",
    "tpg.com.au", "internode.on.net", "westnet.com.au", "dodo.com.au",
    "live.co.uk", "hotmail.co.uk", "yahoo.co.uk", "btinternet.com", "sky.com",
    "virginmedia.com", "talktalk.net", "orange.fr", "wanadoo.fr", "free.fr",
    "laposte.net", "sfr.fr", "gmx.de", "gmx.net", "web.de", "t-online.de",
    "freenet.de", "libero.it", "virgilio.it", "alice.it", "tiscali.it",
    "terra.es", "telefonica.net", "seznam.cz", "centrum.cz", "volny.cz",
    "wp.pl", "onet.pl", "interia.pl", "o2.pl", "freemail.hu", "citromail.hu",
    "mail.ru", "yandex.ru", "rambler.ru", "abv.bg", "mynet.com", "hotmail.fr",
    "hotmail.it", "hotmail.es", "yahoo.fr", "yahoo.de", "yahoo.it", "yahoo.es",
    "yahoo.gr", "yahoo.com.tr", "windowslive.com", "me.com", "mac.com",
    "zoho.com", "mail.com", "gmx.com", "inbox.com", "email.com", "usa.com",
    "aim.com", "rocketmail.com", "ymail.com", "googlemail.com", "qq.com",
    "163.com", "126.com", "naver.com", "hanmail.net", "daum.net", "rediffmail.com",
}
ALL_FREEMAIL = set(FREEMAIL) | FREEMAIL_EXTRA

# Particles that belong to a surname rather than splitting it.
PARTICLES = {"de", "del", "della", "di", "da", "dos", "das", "van", "von", "der",
             "den", "ter", "le", "la", "el", "al", "bin", "ibn", "abu", "mac",
             "mc", "st", "san", "santa", "do", "du", "ten", "op"}

# ---------------------------------------------------------------------------
# GIVEN-NAME ALLOWLIST — the actual gate.
#
# The first version of this module gated on a DENYLIST of role words. Replayed
# against five runs that still had their crawls on disk, it produced mostly
# false positives, because "role word" is unbounded across languages:
#   services@examplehvac.com.au   -> "Services"
#   ihre@email.de                   -> "Ihre"      (German "your", a placeholder)
#   recrutement.cp@examplegroup.com          -> "Recrutement Cp"
#   groupsales.mercosur@example-hotels.com -> "Groupsales Mercosur"
#   grants.lb@example-relief.org  -> "Grants Lb"
#   return.advice@example.ngo           -> "Return Advice"
#   brussels@exampleboxhotels.com      -> "Brussels"
# Every one of those would have put a department or a city in the salutation.
#
# Chasing that list in every language the campaigns touch is not winnable. So the
# gate is inverted: a token is a name only if it IS a known given name. Unknown
# tokens are handed to the name-finder agent, exactly as before — this module
# only ever REMOVES agent dispatches it can answer with evidence, it never
# widens what ships.
#
# Coverage follows the campaign footprint: anglophone (AU/US/GB/IE), German,
# French, Italian, Spanish, Portuguese, Dutch, Nordic, Czech/Slovak, Polish,
# Hungarian, Romanian, Greek, Turkish, and Arabic transliterations (LB/GCC).
# ---------------------------------------------------------------------------
_GIVEN_NAMES_RAW = """
aaron abbie abby abdallah abdel abdo abdul abdullah abe abel abigail abraham
achille ada adam addison adela adele adam adnan adrian adriana adriano adrien
agata agnes agnieszka ahmad ahmed aida aidan aileen aimee aisha aitor ajay akram
alaa alain alan alastair alba albert alberto albrecht aldo alec alejandro
aleksandar aleksandra alessandra alessandro alex alexander alexandra alexandre
alexandru alexei alexia alexis alfie alfonso alfred alfredo ali alice alicia
alina aline alison alistair allan allen allison alma alois alonso alp alper
alvaro alvin alyssa amal amanda amar amaury amber amelia amelie amin amina amir
amira amos amparo amy ana anabel anastasia anatoly andre andrea andreas andrei
andres andrew andrzej andy anete angel angela angelica angelina angelo angus
anita anja anke ann anna annabel annabelle anne anneke annemarie annette annie
anouk anselm anthony antoine anton antonella antonia antonio antony anuj anya
apostolos april arda arek ariana ariel arjan arjun armin arnaud arne arnold
aron arsen art arthur artur arturo arun asa asdrubal ashleigh ashley asif asim
asma astrid athanasios attila aude audrey august augusto aurelie aurelio austin
ava avi axel ayla ayse ayşe azra bahar balazs baldwin barbara barnaby barry
bart bartek bartlomiej basil basma bassam bea beata beatrice beatriz becky
belen ben benedetta benedict benedikt benjamin bennett benoit berat bernadette
bernard bernardo bernd bert bertrand beth bethany betsy bettina betty bianca
bilal bill birgit bjorn blaise blake blanca bo bob bogdan boris boyd brad
bradley brady bram branden brandon brenda brendan brent bret brett brian
bridget brigitte brittany brooke bruce bruno bryan bryce burak burcu byron
caleb callum calvin cameron camila camille candice cara carey carl carla carlo
carlos carmen carol carolina caroline carrie carsten casey caspar cassandra
catalina caterina catherine cathy cecile cecilia cedric celal celia celine
cesar chad chantal charles charlie charlotte chelsea cheryl chiara chloe chris
christa christian christiane christina christine christoph christophe
christopher cindy claire clara clarissa claude claudia claudio clay clement
clifford clint clive cody colin colleen conner connor conor conrad constance
constantin constantine cora coral corey corinne cornelia cosimo courtney craig
cristian cristina cristobal curtis cyril cynthia dagmar dale dalia dalton damian
damien damon dan dana daniel daniela daniele danielle danilo danny dante daphne
dara darcy daria dario darius darlene darren darrell dave david davide davin
dawn dawid dean deborah declan dee deirdre delia demetrio denis denise dennis
derek desmond diana diane diego dieter dilek dimitri dimitrios dina dino dirk
divya dmitri dominic dominik dominique don donald donna dora dorian doris
dorota dorothy doug douglas drew duarte dudley duncan dursun dustin dylan
eamon earl ebru ed eddie eden edgar edith edmund eduardo edward edwin efe
egbert eileen eirik ela elaine elena eleni eleonora elias elie elif elin
elinor eliot elisa elisabeth elise eliza elizabeth ella ellen elliot elliott
elly elmar eloise elsa elvira emanuel emanuele emeka emil emilia emiliano
emilie emilio emily emin emine emma emmanuel emre enda enis enrico enrique
enzo eoin eric erica erich erik erika erin ernest ernesto errol erwin esin
esme esperanza esra essam esteban estelle esther ethan etienne eugene eugenia
eva evan evangelos eve evelien evelyn everett ewa ewan fabian fabien fabio
fabrice fadi faisal faith farah farhan fatih fatima fatma federica federico
felicia felipe felix fenna ferdinand fergus fernanda fernando ferran figen
filip filippo fiona firas flavia flavio fleur florence florent florian
floriane fran frances francesc francesca francesco francis francisco franco
francois frank franka franz fred frederic frederick frederik frida frieda
friedrich fritz gabor gabriel gabriela gabriele gabriella gaby gael gail
galina gareth garry garth gary gaspar gavin gemma gene genevieve geoff geoffrey
george georgia georgina georgios gerald gerard gerd gerhard germain gerrit
gertrude gian gianluca gianni gilbert gilles gina ginevra giorgio giovanna
giovanni giselle gisela giulia giulio giuseppe glen glenn gloria gokhan
gonzalo goran gordon grace graeme graham grant greg gregor gregory greta
griselda guido guillaume guillermo gulcan gunnar gunter gustav gustavo guy
gwen gwendolyn hadi hafsa hakan hakim hal haley halil hamid hamza hana hanna
hannah hannes hans harald harold harriet harry hasan hassan hattie hayden
hayley hazel heather hector heidi heike heiko helen helena helene helga
helmut henk henri henrietta henrik henry herbert herman hermann hernan hester
hilary hilda hisham hoda holger holly hope horst howard hubert hugh hugo huseyin
hussein hydar iain ian ibrahim ida idris ignacio igor ihab ike ilaria ilias
ilona ilse imad iman imogen ina ines inga ingrid ioannis iolanda ion irena
irene iris irma isaac isabel isabella isabelle isaiah isadora ismail israel
issam ivan ivana ivo iwona jacek jack jackie jackson jacob jacqueline jacques
jade jaime jake james jamie jan jana jane janet janice janine janusz jared
jaroslav jarrod jason jasper javier jay jayne jean jeanette jeanne jeff
jeffrey jelena jem jenna jennifer jenny jens jeremy jerome jerry jesper jesse
jessica jesus jill jim jimmy jiri jo joachim joan joanna joanne joao joaquim
joaquin jocelyn jodie joe joel joeri johan johann johanna johannes john johnny
jon jonas jonathan joni joost jordan jordi jorge jorn jos jose josef josefina
joseph josephine josh joshua josiane josie jozef juan juanita judith judy
juha julia julian juliana julie julien juliet julio julius june juraj jurgen
justin justine justyna jutta kai kaitlyn kamil kamila kane kara karel karen
karim karin karina karl karla karolina kaspar kate katerina katharina
katherine kathleen kathrin kathryn kathy katia katie katja katrin katrina
kay kayla keegan keith kelly kelsey ken kendra kenneth kent kerem keri kerry
kevin khaled khalid khalil kiara kieran kim kimberly kirsten kirsty klaas
klaus konrad konstantin kostas krishna kris krista kristen kristian kristin
kristina krystyna krzysztof kurt kyle kylie lachlan laila lana lara larissa
larry lars latifa laura laurel lauren laurence laurent laurie lawrence layla
lea leah leandro lee leen leif leigh leila lena lennart leo leon leonard
leonardo leonie leopold leroy les leslie lester leticia levent levi lewis
lia liam lidia liesbeth lieselotte lila lilian lilias lily lina linda lindsay
lindsey linus lionel lisa lise liselotte liv livia liz lizzie logan lois lola
lorena lorenzo loretta lori lorna lorraine lotte louis louisa louise lourdes
luc luca lucas luce lucia luciano lucie lucien lucinda lucy ludovic ludwig
luigi luis luisa luiz luka lukas luke lut lydia lynn lynne maarten mabel
maciej madeleine madeline madison mads magda magdalena maggie magnus maha
mahmoud maik maike maja majid maksim malcolm malgorzata malik malin mandy
manfred manon manuel manuela mara marc marcel marcela marcelo marci marco
marcos marcus marek margaret margarita margaux margit margot maria mariam
marian mariana marianne mariano maribel marie mariel marija marika marilyn
marina mario marion marisa marisol marius marjan marjolein mark marko markus
marlene marnie marta martha marthe marti martin martina martine marty marvin
mary maryam masood massimo mateo mateusz mathias mathieu mathilde matias
matilda matt matteo matthew matthias matthieu maud maura maureen maurice
mauricio mauro max maxence maxim maxime maximilian maya mayra medhi meg megan
mehmet mehdi mei melanie melinda melis melissa mercedes meredith merve meryem
mia micaela michael michaela michal michalis micheal michel michele michelle
miguel mihai mikael mike mikkel mila milan milena miles milos mina mira
miranda mirco mireille miriam mirko mirjam misha mitchell moe mohamed mohammad
mohammed moira mona monica monika monique morgan moritz morten moses mostafa
mounir muhammad muhammed murat murray mustafa myles myriam nabil nada nadia
nadine nagy naomi naser natalia natalie natasha nathalie nathan nazan neal
ned neil nele nell nelson nerea nerissa nesrin nestor nevin niall nick nickolas
nicky nico nicola nicolas nicole niels nigel nihal niina nikita niklas niko
nikola nikolaos nikos nils nina nino noah noel noelia noemi nora norbert norma
norman nour nuno nurit nuria oceane octavio odile ofelia olaf ole oleg olga
oliver olivia olivier ollie omar ondrej onur ophelie oren orhan orla orlando
oscar osman oskar otto owen ozan ozge pablo paola paolo pascal pascale
pasquale patricia patrick patrik patrizia patti paul paula paulina pauline
paulo pavel pawel pedro peggy pelin penelope penny per pere perry pete peter
petra petros petya phil philip philipp philippe phoebe pia pierre pieter
pietro piotr pip pippa polly poul prakash pratik preben priscilla priya
przemyslaw quentin quinn rachel radu rafael rafal raffaele rahel raimund
raina rainer raj rajesh ralf ralph ramon ramona rana randall randy raoul
raphael rasmus raul ravi ray raymond rebecca rebekka reed reem regina reginald
reinhard reinhold remco remi remy rena renata renate rene renee reto reuben
rex rhys ricardo riccardo richard rick rico rita rob robbie robert roberta
roberto robin rocco rocio rodney rodolfo rodrigo roel roger roland rolf roman
romain romana romeo ron ronald ronan roni ronnie rory rosa rosalie rosanna
rose rosemary rosie ross rowan roxana roy ruben rudolf rudy rui rupert russell
ruth ruud ryan sabina sabine sabrina sacha sadie safa saeed sahar said sally
salma salvatore sam samantha sami samir samuel sanaa sandra sandrine sandro
sanne santiago sara sarah sasha saskia scott sean sebastian sebastien selena
selim selin selma serge sergey sergio serkan seth severine seyma shadi shane
shannon sharon shaun shauna sheila shelley sherif sherry shirley sian sibel
sibylle sid sidney siegfried sienna silke silvana silvia silvio simon simona
simone sinan sinead sixten sofia sofie sohail solange soledad sonia sonja
sophia sophie soraya spencer stacey stan stanislaw stefan stefania stefano
steffen stein stella stephan stephane stephanie stephen sterling steve steven
stewart stian stig stijn stuart sue suleyman sultan susan susana susanna
susanne suzanne svein sven svenja sybille sydney sylvain sylvia sylvie tadeusz
tahir talal tamara tamer tania tanja tanya tara tarek tariq tatiana taylor
ted tenzin terence teresa terry tessa thea thelma theo theodore theresa
therese thibault thierry thomas thorsten tia tibor tiffany tim timo timothy
tina tobias toby todd tom tomas tomasz tommaso tommy toni tony tore torsten
tracey tracy travis trent trevor tristan trudy tuba tugba turgut tyler tyrone
ugo ulf ulla ulrich ulrike umberto ursula uwe vadim valentin valentina
valeria valerie vanessa vasile vasilis vera veronica veronika vicente vicki
victor victoria vincent vincenzo viola violeta virginia vito vittorio vivian
viviane vladimir volker wael walid walter wanda ward warren wayne wendy werner
wesley wiktor wilfried wilhelm will willem william willy wim winston wojciech
wolfgang wouter xander xavier yael yan yannick yara yasemin yasin yasmin
yasmine yassine yavuz yehia yigit ylva yolanda yousef youssef yuki yuri yusuf
yves yvette yvonne zac zach zachary zack zahra zaid zainab zak zeynep zita
ziyad zoe zofia zoltan zuzana
"""
GIVEN_NAMES = {n for n in _GIVEN_NAMES_RAW.split() if n}

# Placeholder locals in the campaign languages ("your email" in the local part).
PLACEHOLDER_EXTRA = {"ihre", "ihr", "deine", "dein", "votre", "vos", "ton",
                     "tuo", "tua", "su", "tu", "vuestro", "seu", "sua", "twoj",
                     "vas", "vase", "din", "ditt", "jouw", "uw", "eposta",
                     "epost", "correo", "courriel", "mailadresse", "adresse"}
# Domains that are a form placeholder, not a real mail host, in any TLD:
# email.de / email.com / mail.example / domain.fr / beispiel.de …
PLACEHOLDER_DOMAIN_RE = re.compile(
    r"^(e?mail|domain|yourdomain|example|exemple|ejemplo|esempio|beispiel|"
    r"sample|test|demo|placeholder|company|firma|site|website|inbox|xyz|abc)\.",
    re.IGNORECASE)

_ALPHA = re.compile(r"[^a-z]")


def _tok(s: str) -> str:
    return _ALPHA.sub("", (s or "").lower())


def brand_tokens(business: str, domain: str) -> set[str]:
    """Words that identify the BUSINESS, so they can never identify a person.

    Covers `minerva@minerva-example.ro` (domain token) and `howardhotel@exampleliving.com`
    (business-name token) — both of which a role-word list alone lets through.
    """
    out: set[str] = set()
    for w in re.findall(r"[a-z]{2,}", (business or "").lower()):
        out.add(w)
    host = root_domain(domain) if domain else ""
    stem = host.split(".")[0] if host else ""
    if stem:
        out.add(stem)
        # a glued domain stem also blocks its own substrings-as-words
        for w in re.findall(r"[a-z]{3,}", stem):
            out.add(w)
    return out


def is_freemail(addr: str) -> bool:
    dom = (addr or "").lower().partition("@")[2]
    return dom in ALL_FREEMAIL or root_domain(dom) in ALL_FREEMAIL


# Words that introduce a PERSON on a business page. Used to corroborate a
# single-token local part: "Owner Sarah", "Founded by Dave", "Ask for Tom".
PERSON_CUE = re.compile(
    r"(?:owner|owned\s+by|founder|founded\s+by|co-?founder|director|managing\s+director|"
    r"proprietor|principal|partner|ceo|cfo|coo|manager|management|head\s+of|"
    r"licensee|licenced\s+by|licensed\s+by|contact|speak\s+(?:to|with)|ask\s+for|"
    r"call|talk\s+to|meet|led\s+by|run\s+by|mr\.?|mrs\.?|ms\.?|dr\.?)"
    r"[\s:,\-–—]{1,4}([A-Z][a-z]{1,19})", re.I)

_CAP_PAIR = re.compile(r"\b([A-Z][a-z]{1,19})\s+([A-Z][a-z]{1,19})\b")


def _page_names(pages_text: str) -> set[str]:
    """Tokens the scraped pages actually present as a PERSON's given name.

    A bare "capitalised word somewhere on the page" is far too weak — it accepts
    `budapest@examplehotel.hu` off the phrase "Budapest city centre". So a token
    only counts when the page shows it in one of the two shapes a person's name
    actually takes:

      1. first word of a `Capitalised Capitalised` pair  ("Neil Baker")
      2. immediately after a person cue                  ("Owner: Sarah")

    Both are free — this text came off disk from the Stage 4 crawl.
    """
    if not pages_text:
        return set()
    out: set[str] = set()
    for m in _CAP_PAIR.finditer(pages_text):
        out.add(m.group(1).lower())
    for m in PERSON_CUE.finditer(pages_text):
        out.add(m.group(1).lower())
    return out


def derive(email: str, business: str = "", domain: str = "",
           pages_text: str = "") -> dict | None:
    """Parse a person's name out of `email`, or return None.

    Returns {first_name, last_name, basis, evidence} on success. `last_name` may
    be "". A returned first_name is enough for the salutation contract
    ("Hello <First>,") per the 2026-07-22 one-name directive; no gender is
    inferred, so `title` is always left empty for the caller.
    """
    e = (email or "").strip().lower()
    if not e or e.count("@") != 1:
        return None
    local, _, dom = e.partition("@")
    if not local or "." not in dom:
        return None
    # A URL-encoded blob scraped out of a JSON island is not an address
    # (observed: `%5b%7b%22description.content%22…@internationalmedicalcorps.org`).
    if "%" in e or len(local) > 40:
        return None
    if is_freemail(e):
        return None                       # personal mailbox, rejected by the email gate anyway
    # ROLE_RE is anchored `^(info|…)$` against the LOCAL PART, so it must be
    # matched on `local`. Matching it on the full address never fires — that is
    # how `services@examplehvac.com.au` became "Services".
    if ROLE_RE.match(local):
        return None
    if local in PLACEHOLDER_LOCALS or local in PLACEHOLDER_EXTRA:
        return None
    if PLACEHOLDER_DOMAIN_RE.match(dom):
        return None
    if any(bad in e for bad in ("example", "yourname", "yourdomain", "j.doe",
                                "johndoe", "janedoe", "placeholder", "@test.",
                                "@domain.", "@sample.")):
        return None

    brand = brand_tokens(business, domain or dom)
    parts = [p for p in re.split(r"[._\-+]", local) if p]
    toks = [_tok(p) for p in parts]
    toks = [t for t in toks if t]
    if not toks:
        return None
    # Any role/brand/placeholder token anywhere disqualifies the whole address:
    # `res.sliema@`, `reception.qld@`, `santa.tibor@thermalspa-example.hu` (santa = brand).
    for t in toks:
        if (t in NOT_A_NAME or t in PLACEHOLDER_LOCALS or t in PLACEHOLDER_EXTRA
                or len(t) < 2):
            return None

    # --- two-token `first.last` -----------------------------------------------
    # The dotted pattern IS the evidence; no page corroboration required.
    real = [t for t in toks if t not in PARTICLES]
    if len(parts) >= 2 and len(real) >= 2:
        first, last = real[0], real[-1]
        # The FIRST name must not be a brand word — `howard.hotel@exampleliving.com`
        # is the property, not a person. The SURNAME is allowed to match the
        # business name: family trades and family hotels are the normal case
        # (`neil.baker@bakertradeservices.com.au` really is Neil Baker), and
        # the existing looks_like_business_name() gate deliberately keeps them
        # too. Both tokens matching the brand is a brand mailbox, not a person.
        if first in brand:
            return None
        # A single-letter first name gives "Hello M," which is not a usable
        # salutation, and a surname alone needs a Mr./Mrs. this parse cannot
        # infer. Leave those to the agent rather than shipping a bad opener.
        if len(first) < 2 or len(last) < 2:
            return None
        # THE GATE: the first token has to be a real given name. Without this,
        # `recrutement.cp@`, `groupsales.mercosur@`, `grants.lb@`,
        # `return.advice@` and `sanmarco.si@` all parse as people.
        if first not in GIVEN_NAMES:
            return None
        return {"first_name": first.capitalize(),
                "last_name": last.capitalize(),
                "basis": "email_local_first_last",
                "evidence": f"local-part '{local}' is a known given name + surname"}

    # --- single token -> first name, ONLY if the site corroborates it ---------
    if len(toks) == 1:
        t = toks[0]
        if t in brand or len(t) < 3 or len(t) > 20:
            return None
        if t not in GIVEN_NAMES:
            return None          # `brussels@`, `antwerp@`, `fiera@`, `ihre@`
        if t in _page_names(pages_text):
            return {"first_name": t.capitalize(),
                    "last_name": "",
                    "basis": "email_local_first_name_site_confirmed",
                    "evidence": f"local-part '{t}' appears as a person name on the scraped pages"}
        return None

    return None


if __name__ == "__main__":       # tiny self-check: python3 name_from_email.py
    CASES = [
        # (email, business, pages, expect_first)
        ("neil.baker@bakertradeservices.com.au", "Hewitt Trade Services", "", "Neil"),
        ("todd@toddbrownplumbing.com.au", "Todd Brown Plumbing", "Todd Petrie founded", None),   # brand token
        ("sarah@bluegateelectrical.com.au", "Blue Gate Electrical", "Owner Sarah has 20 years", "Sarah"),
        ("sarah@bluegateelectrical.com.au", "Blue Gate Electrical", "no person here", None),
        ("budapest@examplehotel.hu", "Example Hotel Buda", "Budapest city centre", None),
        ("recepce@hotelexample.com", "Hotel Example", "", None),
        ("fom@examplehotel.hu", "Example Hotel", "", None),
        ("res.sliema@examplehotels.mt", "The Example Hotel Sliema", "", None),
        ("minerva@minerva-example.ro", "Hotel Minerva", "", None),
        ("you@email.com", "Whatever", "", None),
        ("j.doe@inbox.com", "Whatever", "", None),
        ("rjexample@live.com.au", "RJE Electrical", "", None),          # freemail
        # single-initial first name -> "Hello M," is not shippable; agent's job
        ("m.van.dijk@examplehotel.nl", "Example Hotel", "", None),
        ("info@anything.com", "Anything", "", None),
        ("reception.qld@exampleindustries.com.au", "Baker Trade Services", "", None),
        # family business: surname == business name, first name is real
        ("dave.smith@smithplumbing.com.au", "Smith Plumbing", "", "Dave"),
        # brand mailbox shaped like first.last
        ("howard.hotel@exampleliving.com", "Howard Hotel", "", None),
        ("sales.nsw@acmeelectrical.example.au", "Acme Electrical", "", None),
        # --- every false positive the first (denylist-only) version produced,
        # --- replayed from the five runs that still had raw_html on disk ---
        ("services@examplehvac.com.au", "Example HVAC Group", "Our Services Page", None),
        ("ihre@email.de", "Some Hotel", "Ihre Anfrage", None),
        ("recrutement.cp@examplegroup.com", "Example Group", "", None),
        ("groupsales.mercosur@example-hotels.com", "Example Hotels", "", None),
        ("grants.lb@example-relief.org", "Example Relief", "", None),
        ("return.advice@example.ngo", "Example NGO", "", None),
        ("sanmarco.si@examplechain.it", "Example Chain", "", None),
        ("brussels@exampleboxhotels.com", "Examplebox Hotels", "Brussels Airport shuttle", None),
        ("antwerp@exampleaparts.be", "Example Aparts", "Antwerp Central station", None),
        ("fiera@exsavhotel.com", "Exsav Hotel", "Fiera District nearby", None),
        # --- true positives that must survive the allowlist ---
        ("svenja.fels@parkhotel-rheinau.de", "Rheinau Parkhotel", "", "Svenja"),
        ("camille.mutti@lakesidehotels.ch", "Lakeside Hotels", "", "Camille"),
        ("maya.bedin@hotelesempio.it", "Hotel Esempio", "", "Maya"),
        ("elena.cignini@exhotels.com", "EX Hotels", "", "Elena"),
        ("cecilia.roselli@nordaid.no", "Nord Aid", "", "Cecilia"),
        ("galina.kolesnikova@globalcouncil.org", "Global Council", "", "Galina"),
        # URL-encoded JSON blob scraped as an address
        ("%5b%7b%22description.content%22%3aplease.contact@example.org", "Example", "", None),
    ]
    bad = 0
    for email, biz, pages, want in CASES:
        got = derive(email, biz, email.partition("@")[2], pages)
        first = got["first_name"] if got else None
        ok = (first == want)
        bad += not ok
        print(f"{'ok ' if ok else 'FAIL'} {email:46} -> {str(first):10} (want {want})")
    print(f"\n{len(CASES) - bad}/{len(CASES)} passed")
    sys.exit(1 if bad else 0)
