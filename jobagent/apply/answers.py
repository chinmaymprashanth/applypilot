"""Decide what goes in each application field, from its label alone.

Rules are checked in order: opt-outs and self-identification first, then legal consents (never
ticked), then everything else. Values come only from profile.yaml, answers.yaml and
master_resume.yaml; a blank value leaves the field for you.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..profile import MasterResume, Profile

# Optional contact (texts, marketing emails): answered with answers.yaml's optional_contact (a "no").
OPTIONAL_CONTACT = re.compile(r"\bsms\b|text messag|marketing communications|receive updates|recruitment marketing", re.I)
# Never filled: legal attestations, consents and pronouns are the user's own to give.
NEVER = re.compile(
    r"pronoun|criminal|convicted|felony|background check|drug test|attest|certify|acknowledge|i agree|"
    r"terms|privacy|consent|signature",
    re.I,
)
FOLLOW_UP = re.compile(r"^(\(optional\)\s*)?(if (yes|no|so|not|other|applicable)\b|if (you )?(selected|answered|chose|checked)"
                       r"|if [\"'“])", re.I)
# Voluntary self-identification, from answers.yaml's eeo section. Order matters ("transgender" before "gender").
EEO_RULES = [
    (r"transgender", "transgender"), (r"sexual orientation", "sexual_orientation"), (r"gender|\bsex\b", "gender"),
    (r"hispanic|latin[oa]", "hispanic"), (r"\brace\b|racial|ethnic", "race"), (r"veteran", "veteran"),
    # Only questions about your own service: "Can you support our military customers?" is not one.
    (r"(served|serving|service member|member of|veteran).{0,40}(armed forces|military)|(armed forces|military) "
     r"(service|status|spouse)|transitioning service member", "military"), (r"disabilit", "disability"),
]


@dataclass
class Answer:
    key: str              # what the field is (e.g. "email", "work_authorization", "never", "unknown")
    value: str | None     # what to put in it; None = leave for the user
    note: str = ""        # shown to the user in the checklist


def _yes(flag: bool) -> str:
    return "Yes" if flag else "No"


def answer_for(label: str, kind: str, profile: Profile, answers: dict, master: MasterResume,
               company: str, options: list[str] | None = None) -> Answer:
    """kind: text, textarea, select, combobox, radio, yesno, checkbox or file."""
    # "* First name (Optional)" -> "first name": required-field stars and "(optional)" don't change the question.
    l = " ".join(re.sub(r"\(optional\)|✱", " ", label.lower()).split()).lstrip("*• ").strip()
    first, _, last = profile.name.partition(" ")
    last = profile.name.split()[-1]
    a = lambda k: (answers.get(k) or None)  # noqa: E731 - blank in answers.yaml means "leave it"

    choice = kind in ("select", "combobox", "radio", "yesno", "checkbox", "dropdown")
    if OPTIONAL_CONTACT.search(" ".join([l, *(options or [])])):
        return Answer("optional_contact", a("optional_contact"), "declining optional texts/marketing")
    # "If yes, what type of sponsorship?": a follow-up box to an earlier choice, not a question of its own.
    if FOLLOW_UP.search(l):
        return Answer("follow_up", None, "follow-up to an earlier answer; left for you")
    eeo = answers.get("eeo") or {}
    for pattern, key in EEO_RULES:
        if re.search(pattern, l):
            value = eeo.get(key) or None
            return Answer(f"eeo_{key}", value, "" if value else f"no eeo.{key} in answers.yaml; left for you")
    if re.search(r"non-?compete|nondisclosure|confidentiality agreement", l):
        return Answer("noncompete", a("noncompete"), "")
    if re.search(r"salary|compensation|pay expectation|desired pay|expected pay", l):
        value = None if choice else a("salary_expectation")
        return Answer("salary", value, "salary choice list; pick a range yourself" if choice else "")
    if "pronoun" in l:
        return Answer("pronouns", a("pronouns"), "" if a("pronouns") else "add pronouns to answers.yaml, or type them")
    if NEVER.search(l) or any(NEVER.search(o) for o in options or []):
        return Answer("never", None, "left for you to tick (legal consent or attestation)")
    if kind == "file":
        if "autofill" in l:  # parsing the resume makes the site rewrite the form under us
            return Answer("skip", None)
        if re.search(r"photo|picture|avatar|headshot", l):
            return Answer("skip", None)
        if "cover" in l:
            return Answer("cover_letter_file", "cover_letter")
        if re.search(r"resume|cv|attach", l):
            return Answer("resume_file", "resume")
        return Answer("unlabeled_file", None, "file upload left for you")
    completed = [ed for ed in master.education if not ed.only_for]  # degrees already earned
    rules: list[tuple[str, str, str | None]] = [
        (r"^first name|given name|^first\b", "first_name", first),
        (r"^last name|family name|surname|^last\b", "last_name", last),
        (r"preferred (first )?name and last name|preferred full name", "preferred_name", profile.name),
        (r"preferred (first )?name", "preferred_name", first),
        (r"^(full )?name\*?$|^legal name|^your name|full legal name|legal full name", "full_name", profile.name),
        (r"e-?mail", "email", profile.email),
        (r"phone device type|phone type|type of phone", "phone_type", "Mobile"),
        (r"phone|mobile", "phone", profile.phone),
        (r"linkedin", "linkedin", profile.linkedin),
        (r"github", "github", profile.github),
        (r"website|portfolio|personal (site|url)|other (link|url)", "website", profile.website or profile.github),
        (r"^country", "country", "United States"),
        (r"authori[sz]ed to work|eligible to work|legally (able|permitted)|right to work",
         "work_authorization", _yes(profile.us_citizen or not profile.needs_sponsorship)),
        (r"sponsor|visa", "sponsorship", _yes(profile.needs_sponsorship)),
        (r"u\.?s\.? citizen", "us_citizen", _yes(profile.us_citizen)),
        (r"relocat", "relocation", a("willing_to_relocate")),
        (r"in (the )?office|on-?site|onsite|hybrid|days a week|commut", "work_mode", _yes(bool(profile.work_modes))),
        (r"which (u\.?s\.? )?state|state (do you|you) (reside|live)|state of residence", "state", a("state")),
        (r"address line 1|street address", "address_line1", a("address_line1")),
        (r"address line 2", "address_line2", None),
        (r"postal code|zip", "postal_code", a("postal_code")),
        (r"^city\b|^town\b", "city", (profile.location or "").split(",")[0].strip() or None),
        (r"location|city|where are you (based|located)|\baddress\b", "location", profile.location),
        (r"18 years|over 18|age of 18", "over_18", a("over_18")),
        (r"how did you (first )?(hear|find|learn)|^source\b|referr(ed|al) (by|source)", "referral_source", a("referral_source")),
        (r"start date|available to start|earliest (start|date)|when can you start", "start_date", a("start_date")),
        (r"notice period", "notice_period", a("notice_period")),
        (r"years of (professional |relevant |work )?experience", "years_of_experience", a("years_of_experience")),
        (r"highest (level of )?(degree|education)", "highest_degree", a("highest_degree")),
        (r"current (company|employer)|most recent (company|employer)", "current_company", a("current_company")),
        (r"current (job )?title|most recent (job )?title", "current_title", a("current_title")),
        (r"degree.*(pursuing|working towards|in progress)|(pursuing|current(ly)? enrolled).*degree", "degree_pursuing",
         a("degree_pursuing")),
        (r"school|university|college|institution", "school", a("school") or (completed[0].school if completed else None)),
        (r"graduat", "graduation", a("graduation") if a("graduation") or choice else
         ((completed[0].end or "")[:4] if completed else None)),
    ]
    for pattern, key, value in rules:
        if re.search(pattern, l):
            return Answer(key, value, "" if value else f"no '{key}' in answers.yaml; left for you")

    # "Have you worked here before?" is No unless the company is one of the candidate's employers.
    if re.search(r"(previously|ever|before) (been )?(work|employ|intern)|former employee|worked (for|with|at) us", l):
        mine = any(company.lower() in r.company.lower() or r.company.lower() in company.lower()
                   for r in master.experience)
        return Answer("previous_employee", None if mine else "No | I have not | Never | None",
                      "you worked for this company; answer yourself" if mine else "")
    if kind == "textarea" and re.search(r"cover letter", l):
        return Answer("cover_letter_text", "cover_letter")
    if kind == "textarea" and re.search(r"additional information|anything else", l):
        return Answer("cover_letter_text", "cover_letter")
    if kind in ("textarea", "text") and (kind == "textarea" or "?" in l or len(l) > 40):
        return Answer("free_text", None, "open question")
    return Answer("unknown", None, "couldn't tell what this asks; left for you")
