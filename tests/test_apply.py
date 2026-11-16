import pytest

from jobagent.apply.answers import answer_for
from jobagent.apply.page import GUARD, best_option
from jobagent.apply.runner import fill_form

from test_tailor import LETTER, MASTER, PROFILE

ANSWERS = {"start_date": "immediately", "willing_to_relocate": "Yes", "referral_source": "Company website",
           "current_company": "", "over_18": "Yes"}


def A(label, kind="text", company="SharkNinja"):
    return answer_for(label, kind, PROFILE, ANSWERS, MASTER, company)


@pytest.mark.parametrize("label,kind,key,value", [
    ("First Name*", "text", "first_name", "Alex"),
    ("Last Name*", "text", "last_name", "Rivera"),
    ("Name", "text", "full_name", "Alex Jordan Rivera"),
    ("Email*", "text", "email", "c@example.com"),
    ("Country*", "combobox", "country", "United States"),
    ("Location (City)*", "combobox", "location", "Los Angeles, CA"),
    ("LinkedIn Profile (indicate N/A if you don't have one)", "text", "linkedin", "https://www.linkedin.com/in/c"),
    ("Are you legally authorized to work in the United States?*", "combobox", "work_authorization", "Yes"),
    ("Will you now or in the future require sponsorship for employment visa status (e.g., H-1B)", "combobox",
     "sponsorship", "No"),
    ("Are you comfortable to be in the office specified on the job description, 3 days a week?*", "combobox",
     "work_mode", "Yes"),
    ("Are you willing to relocate to this location?", "radio", "relocation", "Yes"),
    ("Have you previously worked with us as an intern or co-op student at SharkNinja? *", "combobox",
     "previous_employee", "No | I have not | Never | None"),
    ("How did you hear about this job?", "textarea", "referral_source", "Company website"),
    ("Resume/CV resume", "file", "resume_file", "resume"),
    ("Cover Letter cover_letter", "file", "cover_letter_file", "cover_letter"),
])
def test_answers_from_real_form_labels(label, kind, key, value):
    a = A(label, kind)
    assert (a.key, a.value) == (key, value)


ANSWERS.update({"noncompete": "No", "salary_expectation": "Open; the posted range works for me",
                "optional_contact": "No | Decline", "current_company": "IBM",
                "eeo": {"gender": "Male | Man", "transgender": "No", "sexual_orientation": "Heterosexual | Straight",
                        "hispanic": "No", "race": "South Asian | Asian Indian | Asian",
                        "veteran": "I am not a protected veteran | No", "military": "No",
                        "disability": "No, I do not | No"}})


@pytest.mark.parametrize("label,key,value", [
    ("How would you describe your gender identity? (mark all that apply)", "eeo_gender", "Male | Man"),
    ("Do you identify as transgender?", "eeo_transgender", "No"),
    ("How would you describe your sexual orientation? (mark all that apply)", "eeo_sexual_orientation",
     "Heterosexual | Straight"),
    ("How would you describe your racial/ethnic background? (mark all that apply)", "eeo_race",
     "South Asian | Asian Indian | Asian"),
    ("Are you Hispanic/Latino?", "eeo_hispanic", "No"),
    ("Are you a veteran or active member of the United States Armed Forces?", "eeo_veteran",
     "I am not a protected veteran | No"),
    ("Do you have a disability or chronic condition", "eeo_disability", "No, I do not | No"),
    ("Are you subject to any noncompete nondisclosure/confidentiality agreement?*", "noncompete", "No"),
    ("I would like to receive updates from a recruiter about this job via SMS.*", "optional_contact", "No | Decline"),
    ("What is your current company?*", "current_company", "IBM"),
])
def test_self_identification_and_other_answers(label, key, value):
    a = A(label, "combobox")
    assert (a.key, a.value) == (key, value)


def test_salary_text_is_answered_but_choice_lists_are_left():
    assert A("What are your salary expectations?", "text").value == "Open; the posted range works for me"
    assert A("Desired salary range", "select").value is None


@pytest.mark.parametrize("label", ["I agree", "I certify that the information above is true",
                                   "I acknowledge the privacy notice"])
def test_never_ticks_legal_consents(label):
    a = A(label, "checkbox")
    assert a.key == "never" and a.value is None


def test_sms_radio_is_declined_and_autofill_upload_is_not_touched():
    a = answer_for("Phone number", "radio", PROFILE, ANSWERS, MASTER, "X",
                   ["Yes - I consent to receiving text messages", "No - I do not consent"])
    assert (a.key, a.value) == ("optional_contact", "No | Decline")
    assert answer_for("Autofill from resume", "file", PROFILE, ANSWERS, MASTER, "X").key == "skip"


def test_reformatted_values_count_as_filled():
    from jobagent.apply.page import same_text
    assert same_text("(555) 010-0123", "+1 (555) 010-0123")
    assert same_text("https://www.linkedin.com/in/c/", "https://www.linkedin.com/in/c")
    assert not same_text("", "x") and not same_text("Ale", "Alex")


def test_blank_answer_and_own_employer_are_left_for_user():
    assert answer_for("What is your current company?*", "text", PROFILE, {}, MASTER, "X").value is None
    assert A("Have you ever worked for IBM before?", "radio", company="IBM").value is None


def test_best_option():
    assert best_option(["Select...", "Yes", "No"], "Yes") == 1
    assert best_option(["LinkedIn", "Company Website", "Other"], "Company website") == 1
    assert best_option(["Los Angeles, California, United States"], "Los Angeles") == 0
    assert best_option(["A", "B"], "Yes") is None
    assert best_option(["White/Caucasian", "Asian (Not Hispanic or Latino)"], "Asian") == 1


FORM = """<!doctype html><html><body>
<form id="f" onsubmit="window.submitted = (window.submitted || 0) + 1; return false;">
  <label for="fn">First Name*</label><input id="fn" required>
  <label for="ln">Last Name*</label><input id="ln" required>
  <label for="em">Email*</label><input id="em" type="email" required>
  <label for="ph">Phone</label><input id="ph" type="tel">
  <label for="web">Website</label><input id="web">
  <label for="sp">Will you require visa sponsorship?</label>
  <select id="sp"><option>Select...</option><option>Yes</option><option>No</option></select>
  <fieldset><legend>Are you legally authorized to work in the US?</legend>
    <input type="radio" name="auth" id="a1" value="y"><label for="a1">Yes</label>
    <input type="radio" name="auth" id="a2" value="n"><label for="a2">No</label></fieldset>
  <div class="field"><label>Are you willing to relocate?</label>
    <div><button type="submit" onclick="this.dataset.picked=1">Yes</button><button type="submit">No</button></div></div>
  <label for="g">What is your gender?</label><select id="g"><option></option><option>Female</option><option>Male</option></select>
  <div class="phone"><button type="button">+1</button><label for="mob">Mobile phone</label><input id="mob" type="tel"></div>
  <label for="ok">I agree to the privacy policy</label><input type="checkbox" id="ok">
  <label for="hear">How did you hear about us?</label>
  <select id="hear"><option>Select...</option><option>LinkedIn</option><option>Acme Career Website</option></select>
  <label for="cur">What is your current company?*</label><input id="cur" required>
  <div class="field"><label>Resume/CV</label><input type="file" id="resume"></div>
  <div class="field"><label>Cover Letter</label><input type="file" id="cover_letter"></div>
  <label for="why">Why do you want to work here?</label><textarea id="why" required></textarea>
  <button type="submit" id="go">Submit application</button>
</form></body></html>"""


@pytest.fixture(scope="module")
def page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context()
        ctx.add_init_script(GUARD)
        yield ctx.new_page()
        browser.close()


def test_fills_form_and_never_submits(page, tmp_path):
    (tmp_path / "Alex_Jordan_Rivera_Resume_Acme_SWE.pdf").write_bytes(b"%PDF-1.4 resume")
    (tmp_path / "Alex_Jordan_Rivera_Cover_Letter_Acme_SWE.pdf").write_bytes(b"%PDF-1.4 letter")
    (tmp_path / "Alex_Jordan_Rivera_Cover_Letter_Acme_SWE.txt").write_text(LETTER)
    page.set_content(FORM)
    asked = []

    def drafter(job, questions):
        asked.extend(questions)
        return ["The fraud detection work matches your risk team's problems."]

    r = fill_form(page, {"company": "Acme", "title": "SWE"}, tmp_path, PROFILE, ANSWERS, MASTER, drafter, delay=False)

    v = lambda sel: page.locator(sel).input_value()  # noqa: E731
    assert (v("#fn"), v("#ln"), v("#em"), v("#ph")) == ("Alex", "Rivera", "c@example.com", "+1 (555) 010-0123")
    assert page.locator("#sp").input_value() == "No"
    assert v("#web") == "https://github.com/c"  # no GitHub field on this form, so Website gets GitHub
    assert page.locator("#hear").input_value() == "Acme Career Website"  # "Company website" reworded
    assert page.locator("#a1").is_checked()
    assert page.locator("button:has-text('Yes')").get_attribute("data-picked") == "1"
    assert page.locator("#g").input_value() == "Male"
    assert v("#mob") == "(555) 010-0123"  # the form shows +1 itself
    assert not page.locator("#ok").is_checked()  # legal consent is the user's click
    assert page.evaluate("document.getElementById('resume').files[0].name") == "Alex_Jordan_Rivera_Resume_Acme_SWE.pdf"
    assert page.evaluate("document.getElementById('cover_letter').files[0].name") == "Alex_Jordan_Rivera_Cover_Letter_Acme_SWE.pdf"
    assert asked == ["Why do you want to work here?"] and r.drafted
    assert v("#cur") == "IBM" and any("privacy" in x for x in r.left)
    assert r.screenshot.exists()
    # The Yes button is a real submit button, yet nothing was submitted while filling.
    assert page.evaluate("window.submitted || 0") == 0
    # After filling, the guard is off: once the user answers what was left, their own click submits.
    page.locator("#ok").check()
    page.locator("#go").click()
    assert page.evaluate("window.submitted || 0") == 1


JOB_PAGE = """<!doctype html><html><body><h1>Data Analyst</h1><p>About the role...</p>
<button onclick="window.clicked = 'linkedin'">Apply with LinkedIn</button>
<button onclick="window.clicked = 'easy'">Easy Apply</button>
<button onclick="window.clicked = 'apply'; document.body.innerHTML =
  '<form><label for=e>Email</label><input id=e><label>Resume</label><input type=file id=r></form>'">I'm interested</button>
</body></html>"""


def test_job_page_apply_button_opens_the_form_but_never_easy_apply(page):
    from jobagent.apply.page import collect, is_application_form, open_application
    page.set_content(JOB_PAGE)
    assert not is_application_form(collect(page))
    page = open_application(page)
    assert page.evaluate("window.clicked") == "apply"
    assert is_application_form(collect(page))


def test_page_with_a_form_is_left_alone(page):
    from jobagent.apply.page import open_application
    page.set_content('<form><label for=e>Email</label><input id=e><button onclick="window.clicked=1">Apply now'
                     '</button></form>')
    page.evaluate("window.clicked = 0")
    open_application(page)
    assert page.evaluate("window.clicked || 0") == 0


def test_only_external_apply_buttons_means_nothing_is_clicked(page):
    from jobagent.apply.page import open_application
    page.set_content('<button onclick="window.clicked=1">Easy Apply</button>'
                     '<button onclick="window.clicked=2">Apply with Indeed</button>')
    open_application(page)
    assert page.evaluate("window.clicked || 0") == 0


WORKABLE_LIKE = """<!doctype html><html><body>
<form onsubmit="window.submitted = 1; return false;">
  <label for="fn">* First name</label><input id="fn" required>
  <label for="em">* Email</label><input id="em" required>
  <div class="phone"><button type="button" id="cc" onclick="opts.hidden=false">+91</button>
    <ul id="opts" hidden><li role="option" onclick="cc.innerText='+1'; opts.hidden=true">United States (+1)</li>
      <li role="option">India (+91)</li></ul>
    <label for="ph">Phone (Optional)</label><input id="ph"></div>
  <label for="ad">Address (Optional)</label><input id="ad" value="Bengaluru, India">
  <div><input type="file" id="pic" accept="image/png,image/jpeg"></div>
  <div><input type="file" id="up" accept=".pdf,.docx"></div>
  <div class="q"><span id="q1">Have you graduated in the last 2 years?</span>
    <fieldset role="radiogroup" aria-labelledby="q1"><label><input type="radio" name="g" value="y">YES</label>
      <label><input type="radio" name="g" value="n">NO</label></fieldset></div>
  <div><input type="text"></div>
  <div><label>Cover letter</label><textarea id="cl"></textarea></div>
</form>
<div id="banner" style="position:fixed;inset:0;background:rgba(0,0,0,.4)">
  <button onclick="window.cookies='accepted'; banner.remove()">Accept all</button>
  <button onclick="window.cookies='declined'; banner.remove()">Decline all</button>
</div></body></html>"""


def test_cookie_banner_declined_and_starred_labels_and_pickers(page, tmp_path):
    (tmp_path / "Alex_Jordan_Rivera_Resume_Acme_SWE.pdf").write_bytes(b"%PDF-1.4 resume")
    (tmp_path / "Alex_Jordan_Rivera_Cover_Letter_Acme_SWE.txt").write_text(LETTER)
    page.set_content(WORKABLE_LIKE)
    page.evaluate("window.submitted = 0")  # the window is shared with earlier tests
    fill_form(page, {"company": "Acme", "title": "SWE"}, tmp_path, PROFILE, ANSWERS, MASTER, None, delay=False)
    assert page.evaluate("window.cookies") == "declined"
    assert page.locator("#fn").input_value() == "Alex" and page.locator("#em").input_value() == "c@example.com"
    assert page.locator("#cc").inner_text() == "+1"  # picker switched from +91 to United States
    assert page.locator("#ph").input_value() == "(555) 010-0123"
    assert page.locator("#ad").input_value() == "Los Angeles, CA"  # the site's guess from your IP is corrected
    assert page.evaluate("document.getElementById('up').files[0].name") == "Alex_Jordan_Rivera_Resume_Acme_SWE.pdf"
    assert page.evaluate("document.getElementById('pic').files.length") == 0  # photo box left alone
    from jobagent.apply.page import collect
    labels = [q.label for q in collect(page)]
    assert "Have you graduated in the last 2 years?" in labels  # from aria-labelledby, not "Cover letter"
    assert labels.count("* First name") == 1  # the unlabelled text box isn't mistaken for it
    assert page.evaluate("window.submitted || 0") == 0


def test_fill_this_page_fills_the_next_step_without_overwriting(page, tmp_path):
    from jobagent.apply.runner import fill_current_page
    (tmp_path / "Alex_Jordan_Rivera_Cover_Letter_Acme_SWE.txt").write_text(LETTER)
    step2 = """<form><label for=fn>First Name</label><input id=fn value="Chinny">
      <label for=ln>Last Name</label><input id=ln><label for=em>Email</label><input id=em></form>"""
    out = []
    ctx = page.context.browser.new_context()  # reuse the module's browser
    ctx.add_init_script(GUARD)
    ctx.new_page().set_content("<p>Step 1 done</p>")
    page2 = ctx.new_page()  # the site opened step 2 in a new tab
    page2.set_content(step2)
    browser = {"ctx": ctx}
    fill_current_page(browser, {"company": "Acme", "title": "SWE"}, tmp_path, PROFILE, ANSWERS, MASTER,
                      None, out.append)
    assert browser["page"] is page2
    assert page2.locator("#fn").input_value() == "Chinny"  # what the user typed stays
    assert page2.locator("#ln").input_value() == "Rivera"
    assert out[0].startswith("  filled 2 more fields")
    ctx.close()


@pytest.mark.parametrize("url,ats", [
    ("https://job-boards.greenhouse.io/x/jobs/1", "greenhouse"), ("https://jobs.lever.co/x/1", "lever"),
    ("https://apply.workable.com/x/j/1", "workable"), ("https://acme.wd5.myworkdayjobs.com/x", "workday"),
    ("https://hdde.fa.us2.oraclecloud.com/x", "oracle"), ("https://ice.jibeapply.com/x", "icims"),
    ("https://jobs.smartrecruiters.com/Wise/1", "smartrecruiters"), ("https://www.amazon.jobs/x", "amazon"),
    ("https://careers.acme.com/x", "other"),
])
def test_ats_of(url, ats):
    from jobagent.search.common import ats_of
    assert ats_of(url) == ats


def test_captcha_and_signup_sites_go_to_own_chrome(monkeypatch, tmp_path):
    from jobagent.apply import runner
    (tmp_path / "Alex_Jordan_Rivera_Cover_Letter_Acme_SWE.txt").write_text(LETTER)
    ran, out = [], []
    monkeypatch.setattr(runner.subprocess, "run", lambda cmd, **kw: ran.append(cmd))

    class NoPage:  # the automated browser must not be used for these sites
        def goto(self, *a, **k):
            raise AssertionError("automated browser was used")

    job = {"company": "Acme", "title": "SWE", "apply_url": "https://apply.workable.com/acme/j/1"}
    runner.open_and_fill(NoPage(), job, tmp_path, PROFILE, ANSWERS, MASTER, None, out.append, input, {})
    assert ["open", "-a", "Google Chrome", job["apply_url"]] in ran
    assert ["pbcopy"] in ran and any(c[:2] == ["open", "-R"] for c in ran)
    sheet = "\n".join(out)
    assert "Need sponsorship:" in sheet and "Gender:" in sheet and "Male" in sheet and "| Man" not in sheet


def test_pronouns_state_and_first_learn():
    assert A("Pronouns *").value is None  # not set in answers.yaml: left for the user
    assert answer_for("Pronouns *", "text", PROFILE, {"pronouns": "He/him"}, MASTER, "X").value == "He/him"
    assert answer_for("Which U.S. State or Canadian Province do you reside in?*", "combobox", PROFILE,
                      {"state": "California"}, MASTER, "X").value == "California"
    assert A("How did you first learn about Affirm as an employer? *", "combobox").key == "referral_source"


HIDDEN_PICKER = """<!doctype html><html><body><form>
  <label for="ph">Phone*</label>
  <div class="iti"><button type="button" class="iti__selected-country" style="display:none" aria-label="Select country">
    </button><input id="ph" type="tel"></div>
  <label for="li">LinkedIn Profile</label><input id="li">
</form></body></html>"""


def test_hidden_phone_picker_does_not_stop_the_form(page, tmp_path):
    (tmp_path / "Alex_Jordan_Rivera_Cover_Letter_Acme_SWE.txt").write_text(LETTER)
    page.set_content(HIDDEN_PICKER)
    r = fill_form(page, {"company": "Acme", "title": "SWE"}, tmp_path, PROFILE, ANSWERS, MASTER, None, delay=False)
    assert page.locator("#ph").input_value() == "+1 (555) 010-0123"  # full number: no visible code to rely on
    assert page.locator("#li").input_value() == "https://www.linkedin.com/in/c"  # kept going after the phone
    assert r.screenshot is not None


def test_choose_jobs_before_anything_is_written(tmp_path):
    from jobagent.apply.runner import choose_jobs
    from jobagent.tracker import Tracker

    t = Tracker(tmp_path / "t.xlsx", tmp_path / "jobs")
    for i in range(4):
        t.add_job({"company": f"Co{i}", "title": "Software Engineer", "url": f"https://x.io/{i}", "source": "greenhouse",
                   "apply_url": f"https://job-boards.greenhouse.io/co{i}/jobs/{i}"})
    for jid in t.jobs:
        t.update(jid, status="shortlisted", score=80)
    jobs = t.with_status("shortlisted")
    answers = iter(["r", "a", "n", "l", "q"])
    opened, out = [], []
    chosen = choose_jobs(t, jobs, lambda prompt: next(answers), out.append, opened.append)
    assert [j["company"] for j in chosen] == [jobs[0]["company"]]
    assert opened == [jobs[0]["apply_url"]]  # [r]ead opened the posting, then asked again
    statuses = [t.jobs[j["id"]]["status"] for j in jobs]
    assert statuses == ["shortlisted", "skipped", "shortlisted", "shortlisted"]  # apply / no / later / quit
    assert any("score 80" in line for line in out)
