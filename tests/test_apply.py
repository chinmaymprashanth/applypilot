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


WORKDAY_LIKE = """<!doctype html><html><body><form>
  <div><label for="hear">How Did You Hear About Us?*</label>
    <button type="button" id="hear" aria-haspopup="listbox" aria-label="How Did You Hear About Us? select one required"
      onclick="hearlist.hidden=false">select one</button>
    <ul id="hearlist" role="listbox" hidden>
      <li role="option" onclick="hear.innerText='LinkedIn'; hearlist.hidden=true">LinkedIn</li>
      <li role="option" onclick="hear.innerText='Company Careers Website'; hearlist.hidden=true">Company Careers Website</li>
    </ul></div>
  <div><label for="g">Gender</label>
    <button type="button" id="g" aria-haspopup="listbox" aria-label="Gender select one" onclick="glist.hidden=false">select one</button>
    <ul id="glist" role="listbox" hidden>
      <li role="option" onclick="g.innerText='Female'; glist.hidden=true">Female</li>
      <li role="option" onclick="g.innerText='Male'; glist.hidden=true">Male</li>
    </ul></div>
  <label for="a1">Address Line 1*</label><input id="a1">
  <label for="zip">Postal Code*</label><input id="zip">
  <label for="pt">Phone Device Type*</label><input id="pt">
</form></body></html>"""


def test_workday_style_dropdowns_and_address(page, tmp_path):
    (tmp_path / "Alex_Jordan_Rivera_Cover_Letter_Acme_SWE.txt").write_text(LETTER)
    page.set_content(WORKDAY_LIKE)
    answers = {**ANSWERS, "address_line1": "1 Main St", "postal_code": "90001"}
    fill_form(page, {"company": "Acme", "title": "SWE"}, tmp_path, PROFILE, answers, MASTER, None, delay=False)
    assert page.locator("#hear").inner_text() == "Company Careers Website"  # "Company website" reworded
    assert page.locator("#g").inner_text() == "Male"
    assert (page.locator("#a1").input_value(), page.locator("#zip").input_value()) == ("1 Main St", "90001")
    assert page.locator("#pt").input_value() == "Mobile"


def test_workday_opens_in_jobagents_browser_for_sign_in():
    from jobagent.apply.runner import OWN_BROWSER, SIGN_IN_FIRST
    assert "workday" not in OWN_BROWSER and "workday" in SIGN_IN_FIRST


# ---- Ashby and Lever, built from their real markup (November 2026) ---------------------------------------------

# Ashby: the whole form is aria-labelledby "Application"; each question is a field entry with its own title.
# Yes/No are button pairs with aria-pressed; React keeps a typed value only once the field loses focus; dropping a
# file in "Autofill from resume" (or the resume field) makes Ashby parse it and rebuild the form.
ASHBY = """<!doctype html><html><body><h2 id="app-title">Application</h2>
<div id="form" aria-labelledby="app-title">
  <div class="ashby-application-form-autofill-input-root"><input type="file" id="autofill"></div>
  <div id="fields"></div>
  <div class="ashby-application-form-field-entry" data-field-path="_systemfield_resume">
    <label class="ashby-application-form-question-title">Resume</label>
    <input type="file" id="_systemfield_resume" onchange="rebuild()"></div>
  <div class="ashby-application-form-field-entry" data-field-path="auth">
    <label class="_required_x ashby-application-form-question-title" for="auth">Are you authorized to work in the United States?</label>
    <div class="_yesno_x"><button id="y" aria-pressed="false" onclick="press(this)">Yes</button>
      <button id="n" aria-pressed="false" onclick="press(this)">No</button><input type="checkbox" tabindex="-1" name="auth"></div></div>
  <div class="ashby-application-form-field-entry" data-field-path="hear">
    <label class="ashby-application-form-question-title">How did you hear about us?</label>
    <input role="combobox" placeholder="Start typing..." id="hear" oninput="opts(this)"><div id="hear-list"></div></div>
  <div class="ashby-application-form-field-entry" data-field-path="cities">
    <label class="ashby-application-form-question-title">Which offices could you work from?</label>
    <label><input type="checkbox" name="c1">Boston, MA</label><label><input type="checkbox" name="c2">Seattle, WA</label></div>
  <div class="ashby-application-form-field-entry" data-field-path="hours">
    <label class="ashby-application-form-question-title" for="hours">How many hours per week can you be in the office?</label>
    <input type="number" id="hours" placeholder="Type here..."></div>
  <input type="text" name="website" id="footer-form-website" tabindex="-1" style="position:absolute;left:-5000px">
</div>
<script>
  window.parsed = 0;
  const FIELDS = `<div class="ashby-application-form-field-entry" data-field-path="name">
      <label for="name">Name</label><input id="name" required></div>
    <div class="ashby-application-form-field-entry" data-field-path="email">
      <label for="email">Email</label><input id="email" required></div>`;
  document.getElementById('fields').innerHTML = FIELDS;
  function rebuild() {  // the resume parser rebuilds the form 300 ms after upload, wiping what was typed
    setTimeout(() => { window.parsed++; document.getElementById('fields').innerHTML = FIELDS; }, 300);
  }
  function press(b) { for (const o of b.parentElement.querySelectorAll('button')) o.setAttribute('aria-pressed', o === b); }
  function opts(i) { document.getElementById('hear-list').innerHTML = i.value ?
      '<div role="option" onclick="pick(this)">Company website</div><div role="option" onclick="pick(this)">LinkedIn</div>' : ''; }
  function pick(o) { document.getElementById('hear').value = o.innerText; document.getElementById('hear-list').innerHTML = ''; }
</script></body></html>"""


def test_ashby_questions_files_first_yes_no_and_traps(page, tmp_path):
    from jobagent.apply.page import collect
    (tmp_path / "Alex_Jordan_Rivera_Resume_Acme_SWE.pdf").write_bytes(b"%PDF-1.4 resume")
    (tmp_path / "Alex_Jordan_Rivera_Cover_Letter_Acme_SWE.txt").write_text(LETTER)
    page.set_content(ASHBY)
    labels = {q.label: q for q in collect(page)}
    # Each question's own title, not the form's "Application" or the "Start typing..." placeholder.
    assert "Are you authorized to work in the United States?" in labels
    assert labels["Are you authorized to work in the United States?"].required
    assert "How did you hear about us?" in labels
    assert labels["Which offices could you work from?"].options == ["Boston, MA", "Seattle, WA"]  # one question
    assert not any("website" in q.label.lower() for q in collect(page))  # the hidden trap field isn't a question

    r = fill_form(page, {"company": "Acme", "title": "SWE"}, tmp_path, PROFILE, ANSWERS, MASTER, None, delay=False)

    assert page.evaluate("window.parsed") == 1  # the resume went in, and the rebuild happened...
    assert page.locator("#name").input_value() == "Alex Jordan Rivera"  # ...before name and email were typed
    assert page.locator("#email").input_value() == "c@example.com"
    assert page.evaluate("document.getElementById('autofill').files.length") == 0  # "Autofill from resume" untouched
    assert page.locator("#y").get_attribute("aria-pressed") == "true"
    assert page.locator("#n").get_attribute("aria-pressed") == "false"
    assert page.locator("#hear").input_value() == "Company website"
    assert page.locator("#hours").input_value() == ""  # "Yes" can't go in a number box: left for you
    assert any("hours per week" in x for x in r.left)
    assert page.locator("#footer-form-website").input_value() == ""  # never fill a honeypot


def test_a_value_the_site_clears_is_typed_again(page, tmp_path):
    (tmp_path / "Alex_Jordan_Rivera_Cover_Letter_Acme_SWE.txt").write_text(LETTER)
    page.set_content("""<label for=em>Email</label><input id=em>
      <label for=ln>Last name</label><input id=ln>
      <script>let once = true;  // the site wipes Email once, after the next field is filled
        document.getElementById('ln').addEventListener('input', () => {
          if (once) { once = false; setTimeout(() => document.getElementById('em').value = '', 50); } });</script>""")
    r = fill_form(page, {"company": "Acme", "title": "SWE"}, tmp_path, PROFILE, ANSWERS, MASTER, None, delay=False)
    assert page.locator("#em").input_value() == "c@example.com"
    assert not r.left


LEVER = """<!doctype html><html><body><form>
<ul><li class="application-question"><label><div class="application-label">Full name</div>
  <div class="application-field"><input name="name" type="text"></div></label></li>
<li class="application-question custom-question"><div><div class="application-label full-width textarea">
  <div class="text">Why are you interested in Acme's robotics team?</div></div>
  <div class="application-field full-width"><textarea class="card-field-input" name="cards[c6][field0]"
   placeholder="Type your response"></textarea></div></div></li></ul></form></body></html>"""


def test_lever_custom_question_is_read_from_its_card(page, tmp_path):
    (tmp_path / "Alex_Jordan_Rivera_Cover_Letter_Acme_SWE.txt").write_text(LETTER)
    page.set_content(LEVER)
    asked = []

    def drafter(job, questions):
        asked.extend(questions)
        return ["I built and shipped LLM agents end to end."]

    r = fill_form(page, {"company": "Acme", "title": "SWE"}, tmp_path, PROFILE, ANSWERS, MASTER, drafter, delay=False)
    assert asked == ["Why are you interested in Acme's robotics team?"]  # the question, not "Type your response"
    assert page.locator("textarea").input_value().startswith("I built")
    assert r.drafted


def test_form_answers_are_saved_for_a_hand_off(page, tmp_path):
    from jobagent.apply.runner import save_form_answers
    page.set_content("""<label for=em>Email</label><input id=em value="c@example.com">
      <label for=why>Why us?</label><textarea id=why>Because of the robots.</textarea>
      <label for=s>Sponsorship?</label><select id=s><option></option><option selected>No</option></select>""")
    text = save_form_answers(page, tmp_path).read_text()
    assert "Email\nc@example.com" in text and "Why us?\nBecause of the robots." in text and "Sponsorship?\nNo" in text


@pytest.mark.parametrize("label,key", [
    ("If yes, what type of sponsorship?", "follow_up"),
    ('If you selected "A protected Individual" OR "Other" to the above question, please explain', "follow_up"),
    ("(Optional) If other, please provide further details", "follow_up"),
    ("Please state your full legal name:", "full_name"),
    ("Legal Full Name", "full_name"),
    ("Have you served in the U.S. armed forces?", "eeo_military"),
    ("Are you a transitioning service member?", "eeo_military"),
])
def test_follow_ups_legal_name_and_military(label, key):
    assert A(label).key == key


def test_supporting_military_customers_is_not_a_self_id_question():
    q = ("Shield AI provides technology, services, and support to the United States military and allied forces. "
         "Are there any reasons why you would not be able to fully contribute in support of the U.S. military?")
    assert not A(q, "radio").key.startswith("eeo")


LEVER_FIELDS = """<!doctype html><html><body><form><ul>
<li class="application-question"><label><div class="application-label">Full name<span class="required">✱</span></div>
  <div class="application-field"><input name="name" type="text"></div></label></li>
<li class="application-question"><label><div class="application-label">Current location</div>
  <div class="application-field"><input class="location-input" id="location-input" name="location" oninput="suggest(this)">
  <div class="dropdown-results"></div></div></label></li>
<li><label>Gender<div class="application-field"><select name="g"><option>Select ...</option><option>Male</option>
  <option>Female</option></select></div></label></li></ul></form>
<script>
  function suggest(i) { const r = i.parentElement.querySelector('.dropdown-results');
    r.innerHTML = i.value.length > 3 ? '<div class="dropdown-location" onmousedown="pick(this)">Los Angeles, CA, USA</div>'
      + '<div class="dropdown-location" onmousedown="pick(this)">Los Ángeles, Biobío, CHL</div>' : ''; }
  function pick(o) { const i = document.getElementById('location-input'); i.value = o.innerText; i.dataset.picked = 1;
    o.parentElement.innerHTML = ''; }
  // Like Lever: typed text that wasn't picked from the list is cleared when you leave the box.
  document.getElementById('location-input').addEventListener('blur', e => { if (!e.target.dataset.picked) e.target.value = ''; });
</script></body></html>"""


def test_lever_name_star_location_suggestions_and_wrapped_labels(page, tmp_path):
    from jobagent.apply.page import collect
    (tmp_path / "Alex_Jordan_Rivera_Cover_Letter_Acme_SWE.txt").write_text(LETTER)
    page.set_content(LEVER_FIELDS)
    assert "Gender" in [q.label for q in collect(page)]  # not "Gender Select ... Male Female"
    r = fill_form(page, {"company": "Acme", "title": "SWE"}, tmp_path, PROFILE, ANSWERS, MASTER, None, delay=False)
    assert page.locator("input[name=name]").input_value() == "Alex Jordan Rivera"
    assert page.locator("#location-input").input_value() == "Los Angeles, CA, USA"  # picked, so it stays
    assert not r.left


def test_preferred_first_and_last_name_is_the_full_name():
    assert A("What is your preferred first name and last name?").value == "Alex Jordan Rivera"
    assert A("Preferred First Name").value == "Alex"


@pytest.mark.parametrize("label", ["Why Acme", "What excites you about this role", "Why do you want to join us?",
                                   "Tell us why you're interested in this position and our mission."])
def test_motivation_questions_are_drafted(label):
    assert A(label, "text").key == "free_text"
    assert A(label, "textarea").key == "free_text"


def test_why_us_drafts_get_the_posting_research_and_letter(page, tmp_path, monkeypatch):
    import json as _json
    from jobagent import llm
    from jobagent.apply.runner import Draft, Drafts, claude_drafter
    (tmp_path / "Alex_Jordan_Rivera_Cover_Letter_Acme_SWE.txt").write_text(LETTER)
    (tmp_path / "job.json").write_text(_json.dumps({
        "description": "Acme builds route-planning software for 3,000 delivery fleets. You'll build Python services.",
        "company_research": ["Acme launched same-day routing for grocery chains in 2026."]}))
    sent = {}

    def fake_parse(client, *, content, **kw):
        sent.update(_json.loads(content))
        return Drafts(answers=[
            Draft(number=1, answer="Acme's routing for 3,000 delivery fleets is the kind of system I want to build; "
                                   "at IBM I built Python services end to end."),
            Draft(number=2, answer="I have 9 years of Kubernetes experience."),  # not in the record: dropped
        ])

    monkeypatch.setattr(llm, "parse", fake_parse)
    page.set_content("""<label for=w>Why do you want to work at Acme?</label><textarea id=w></textarea>
      <label for=k>Describe your container experience.</label><textarea id=k></textarea>""")
    r = fill_form(page, {"company": "Acme", "title": "SWE"}, tmp_path, PROFILE, ANSWERS, MASTER,
                  claude_drafter(None, "m", MASTER, PROFILE), delay=False)
    assert "3,000 delivery fleets" in sent["job_description"]
    assert sent["company_facts"] == ["Acme launched same-day routing for grocery chains in 2026."]
    assert sent["cover_letter"] == LETTER
    assert page.locator("#w").input_value().startswith("Acme's routing")  # company number allowed: it's in the posting
    assert page.locator("#k").input_value() == ""  # an invented skill never reaches the form
    assert r.drafted == ["Why do you want to work at Acme?"]
