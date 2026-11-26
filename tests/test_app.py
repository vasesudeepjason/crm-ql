"""Tests for projects, tasks, time tracking and the Gantt.

Run from the crm-ql folder:

    python -m unittest discover -s tests -v

They use a brand-new database in a temporary folder, never leads.db.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = tempfile.mkdtemp(prefix="crm-tests-")
os.chdir(WORK)   # the app opens "leads.db" in the current folder
os.environ.setdefault("SECRET_KEY", "tests-only")
os.environ["SESSION_COOKIE_SECURE"] = "false"
os.environ["TASK_FILE_DIR"] = os.path.join(WORK, "task_files")
sys.path.insert(0, ROOT)

import app as crm  # noqa: E402

BASE = {"base_url": "http://localhost"}
_counter = [0]


def query(sql, params=()):
    with sqlite3.connect(os.path.join(WORK, "leads.db")) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(sql, params).fetchall()
        db.commit()
        return rows


def insert(sql, params=()):
    with sqlite3.connect(os.path.join(WORK, "leads.db")) as db:
        db.execute("PRAGMA foreign_keys = ON")
        cursor = db.execute(sql, params)
        db.commit()
        return cursor.lastrowid


def make_user(role="telecaller", name=None):
    _counter[0] += 1
    name = name or f"{role.title()} {_counter[0]}"
    return insert("INSERT INTO users (username,password_hash,name,role,created_at,email) VALUES (?,?,?,?,?,?)",
                  (f"user{_counter[0]}", "x", name, role, "2026-01-01 00:00:00", f"user{_counter[0]}@example.com"))


class Session:
    """A signed-in browser for one user."""

    def __init__(self, user_id):
        self.user_id = user_id
        self.client = crm.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = user_id
        self.client.get("/tasks", **BASE)
        with self.client.session_transaction() as session:
            self.token = session["tasks_csrf"]

    def get(self, url):
        return self.client.get(url, **BASE)

    def post(self, url, data=None, token=True):
        data = dict(data or {})
        if token:
            data["token"] = self.token
        return self.client.post(url, data=data, **BASE)


def make_project(owner_id, members=(), name=None, **fields):
    _counter[0] += 1
    columns = {"name": name or f"Project {_counter[0]}", "project_manager_id": owner_id, "status": "Planning",
               "priority": "Medium", "created_at": "2026-01-01 00:00:00", "updated_at": "2026-01-01 00:00:00", **fields}
    project_id = insert(f"INSERT INTO projects ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})", tuple(columns.values()))
    for user_id in {owner_id, *members}:
        insert("INSERT INTO project_members (project_id,user_id,added_at) VALUES (?,?,?)", (project_id, user_id, "2026-01-01 00:00:00"))
    return project_id


def make_task(creator_id, title=None, project_id=None, assignees=(), **fields):
    _counter[0] += 1
    columns = {"title": title or f"Task {_counter[0]}", "status": "To Do", "created_by": creator_id, "project_id": project_id,
               "created_at": "2026-01-01 00:00:00", "updated_at": "2026-01-01 00:00:00", **fields}
    task_id = insert(f"INSERT INTO work_tasks ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})", tuple(columns.values()))
    for user_id in assignees:
        insert("INSERT INTO work_task_assignees (task_id,user_id) VALUES (?,?)", (task_id, user_id))
    return task_id


class PageTest(unittest.TestCase):
    def assertIn(self, needle, haystack, msg=None):
        if isinstance(haystack, str) and len(haystack) > 500:
            self.assertTrue(needle in haystack, msg or f"{needle!r} is missing from the page")
        else:
            super().assertIn(needle, haystack, msg)

    def assertNotIn(self, needle, haystack, msg=None):
        if isinstance(haystack, str) and len(haystack) > 500:
            self.assertFalse(needle in haystack, msg or f"{needle!r} should not be on the page")
        else:
            super().assertNotIn(needle, haystack, msg)


def task_row(task_id):
    return query("SELECT * FROM work_tasks WHERE id=?", (task_id,))[0]


class TaskAccess(PageTest):
    @classmethod
    def setUpClass(cls):
        cls.manager, cls.owner, cls.member, cls.outsider = (make_user("manager"), make_user(), make_user(), make_user())
        cls.project = make_project(cls.owner, [cls.member])
        cls.secret = make_task(cls.owner, "Secret project task", cls.project)
        cls.general = make_task(cls.owner, "General office task")

    def test_outsider_cannot_open_or_change_a_project_task(self):
        outsider = Session(self.outsider)
        self.assertEqual(outsider.get(f"/tasks/{self.secret}").status_code, 404)
        self.assertEqual(outsider.post(f"/tasks/{self.secret}/status", {"status": "Complete"}).status_code, 404)
        self.assertEqual(outsider.post(f"/tasks/{self.secret}/comment", {"body": "hi"}).status_code, 404)
        self.assertEqual(outsider.post(f"/tasks/{self.secret}/timer/start").status_code, 404)
        self.assertEqual(task_row(self.secret)["status"], "To Do")

    def test_outsider_does_not_see_project_tasks_in_any_view(self):
        outsider = Session(self.outsider)
        for url in ("/tasks", "/tasks/board", "/tasks/calendar", "/tasks/gantt"):
            page = outsider.get(url).get_data(as_text=True)
            self.assertNotIn("Secret project task", page, url)
        self.assertIn("General office task", outsider.get("/tasks").get_data(as_text=True))

    def test_team_member_manager_and_assignee_can_open_it(self):
        for user_id in (self.manager, self.owner, self.member):
            self.assertEqual(Session(user_id).get(f"/tasks/{self.secret}").status_code, 200)
        helper = make_user()
        handed = make_task(self.owner, "Handed over", self.project, assignees=[helper])
        self.assertEqual(Session(helper).get(f"/tasks/{handed}").status_code, 200)

    def test_outsider_cannot_file_a_task_under_the_project(self):
        outsider = Session(self.outsider)
        self.assertEqual(outsider.post("/tasks/new", {"title": "Sneaky", "project_id": self.project}).status_code, 400)
        self.assertFalse(query("SELECT 1 FROM work_tasks WHERE title='Sneaky'"))


class Projects(PageTest):
    @classmethod
    def setUpClass(cls):
        cls.manager, cls.staff = make_user("manager"), make_user()
        cls.company = insert("INSERT INTO companies (name,status,created_at,updated_at) VALUES (?,?,?,?)",
                             ("Acme Traders", "Customer", "2026-01-01 00:00:00", "2026-01-01 00:00:00"))

    def new_project(self, session, **extra):
        data = {"name": "Website build", "project_type": "Website", "project_manager_id": self.manager,
                "status": "Planning", "priority": "Medium", **extra}
        return session.post("/projects/new", data)

    def test_create_needs_a_type_and_a_token(self):
        manager = Session(self.manager)
        self.assertEqual(manager.post("/projects/new", {"name": "No token", "project_type": "SEO", "project_manager_id": self.manager,
                                                         "status": "Planning", "priority": "Medium"}, token=False).status_code, 400)
        self.new_project(manager, name="Typeless", project_type="")
        self.assertFalse(query("SELECT 1 FROM projects WHERE name='Typeless'"))
        self.new_project(manager, name="For Acme", company_id=self.company)
        row = query("SELECT * FROM projects WHERE name='For Acme'")[0]
        self.assertEqual((row["project_type"], row["company_id"]), ("Website", self.company))

    def test_edit_changes_details_and_team(self):
        manager = Session(self.manager)
        project = make_project(self.manager, name="Before", project_type="SEO")
        response = manager.post(f"/projects/{project}/edit", {
            "name": "After", "project_type": "Ads", "company_id": self.company, "project_manager_id": self.manager,
            "status": "In Progress", "priority": "High", "budget": "45000", "start_date": "2026-11-01",
            "target_date": "2026-12-15", "description": "Scope", "member_ids": [self.staff]})
        self.assertEqual(response.status_code, 302)
        row = query("SELECT * FROM projects WHERE id=?", (project,))[0]
        self.assertEqual((row["name"], row["project_type"], row["company_id"], row["status"], row["budget"], row["target_date"]),
                         ("After", "Ads", self.company, "In Progress", 45000.0, "2026-12-15"))
        members = {r["user_id"] for r in query("SELECT user_id FROM project_members WHERE project_id=?", (project,))}
        self.assertEqual(members, {self.manager, self.staff})

    def test_staff_cannot_edit_someone_elses_project(self):
        project = make_project(self.manager, [self.staff], name="Locked")
        Session(self.staff).post(f"/projects/{project}/edit", {"name": "Hacked", "project_type": "Ads", "project_manager_id": self.staff,
                                                              "status": "Planning", "priority": "Low"})
        self.assertEqual(query("SELECT name FROM projects WHERE id=?", (project,))[0]["name"], "Locked")

    def test_status_change_needs_a_token(self):
        project = make_project(self.manager)
        manager = Session(self.manager)
        self.assertEqual(manager.post(f"/projects/{project}/status", {"status": "Completed"}, token=False).status_code, 400)
        manager.post(f"/projects/{project}/status", {"status": "Completed"})
        self.assertEqual(query("SELECT status FROM projects WHERE id=?", (project,))[0]["status"], "Completed")


class TimeTracking(PageTest):
    @classmethod
    def setUpClass(cls):
        cls.user = make_user("manager")
        cls.first, cls.second = make_task(cls.user), make_task(cls.user)

    def test_one_running_timer_per_person(self):
        me = Session(self.user)
        me.post(f"/tasks/{self.first}/timer/start")
        me.post(f"/tasks/{self.second}/timer/start")
        running = query("SELECT task_id FROM work_task_time WHERE user_id=? AND ended_at IS NULL", (self.user,))
        self.assertEqual([row["task_id"] for row in running], [self.second])
        me.post("/tasks/timer/stop")
        self.assertFalse(query("SELECT 1 FROM work_task_time WHERE user_id=? AND ended_at IS NULL", (self.user,)))
        self.assertEqual(len(query("SELECT 1 FROM work_task_time WHERE user_id=?", (self.user,))), 2)

    def test_timer_needs_a_token(self):
        self.assertEqual(Session(self.user).post(f"/tasks/{self.first}/timer/start", token=False).status_code, 400)


class TimeReport(PageTest):
    @classmethod
    def setUpClass(cls):
        cls.manager, cls.staff, cls.other = make_user("manager", "Report Manager"), make_user(name="Report Staff"), make_user(name="Report Other")
        company = insert("INSERT INTO companies (name,status,created_at,updated_at) VALUES (?,?,?,?)",
                         ("Zenith Clinics", "Customer", "2026-01-01 00:00:00", "2026-01-01 00:00:00"))
        cls.project = make_project(cls.manager, [cls.staff], name="Zenith SEO", company_id=company)
        cls.task = make_task(cls.manager, "Keyword plan", cls.project, assignees=[cls.staff])
        cls.hidden = make_task(cls.manager, "Board pack", make_project(cls.manager, name="Private project"))

    def test_estimate_accepts_plain_language_and_rejects_junk(self):
        manager = Session(self.manager)
        manager.post(f"/tasks/{self.task}/update", {"title": "Keyword plan", "status": "To Do", "estimate": "2h 30m"})
        self.assertEqual(task_row(self.task)["estimate_minutes"], 150)
        manager.post(f"/tasks/{self.task}/update", {"title": "Keyword plan", "status": "To Do", "estimate": "1.5"})
        self.assertEqual(task_row(self.task)["estimate_minutes"], 90)
        manager.post(f"/tasks/{self.task}/update", {"title": "Keyword plan", "status": "To Do", "estimate": "soon"})
        self.assertEqual(task_row(self.task)["estimate_minutes"], 90)

    def test_manual_time_and_report(self):
        today = datetime.now(crm.APP_TIMEZONE).date().isoformat()
        staff, manager = Session(self.staff), Session(self.manager)
        staff.post(f"/tasks/{self.task}/time", {"duration": "1h 15m", "day": today})
        manager.post(f"/tasks/{self.task}/time", {"duration": "45m", "day": today})
        manager.post(f"/tasks/{self.hidden}/time", {"duration": "3h", "day": today})
        staff.post(f"/tasks/{self.task}/time", {"duration": "2h", "day": "2999-01-01"})   # the future: refused
        self.assertEqual(sorted(r["seconds"] for r in query("SELECT seconds FROM work_task_time WHERE task_id=?", (self.task,))), [2700, 4500])

        report = manager.get("/tasks/time?period=week").get_data(as_text=True)
        for text in ("Report Staff", "Report Manager", "Zenith SEO", "Zenith Clinics", "Private project", "5h tracked"):
            self.assertIn(text, report)
        mine = staff.get("/tasks/time?period=week").get_data(as_text=True)
        self.assertIn("1h 15m tracked", mine)
        self.assertNotIn("Board pack", mine)
        self.assertNotIn('tk-time-name">Report Manager', mine)
        # A person filter the staff member is not allowed to use changes nothing.
        self.assertIn("1h 15m tracked", staff.get(f"/tasks/time?period=week&assignee={self.manager}").get_data(as_text=True))

    def test_project_page_and_filters(self):
        manager = Session(self.manager)
        page = manager.get(f"/projects/{self.project}").get_data(as_text=True)
        self.assertIn("Time and billing", page)
        listing = manager.get("/projects?client=internal&view=details").get_data(as_text=True)
        self.assertNotIn("Zenith SEO", listing)
        self.assertIn("Private project", listing)
        self.assertEqual(manager.get("/projects?type=SEO").status_code, 200)


class Gantt(PageTest):
    @classmethod
    def setUpClass(cls):
        cls.user = make_user("manager")
        cls.session = Session(cls.user)

    def test_every_scale_renders(self):
        today = date.today()
        make_task(self.user, "Dated task", start_date=today.isoformat(), due_date=(today + timedelta(days=4)).isoformat())
        for zoom in ("day", "week", "month", "quarter"):
            response = self.session.get(f"/tasks/gantt?zoom={zoom}")
            self.assertEqual(response.status_code, 200, zoom)
            self.assertIn("Dated task", response.get_data(as_text=True))

    def test_dragging_days_keeps_times_and_rejects_bad_ranges(self):
        task = make_task(self.user, start_date="2026-11-26", start_time="09:00", due_date="2026-11-27", due_time="17:00")
        self.assertEqual(self.session.post(f"/tasks/{task}/dates", {"start_date": "2026-12-01", "due_date": "2026-12-02"}).status_code, 204)
        row = task_row(task)
        self.assertEqual((row["start_date"], row["start_time"], row["due_date"], row["due_time"]),
                         ("2026-12-01", "09:00", "2026-12-02", "17:00"))
        self.assertEqual(self.session.post(f"/tasks/{task}/dates", {"start_date": "2026-12-05", "due_date": "2026-12-02"}).status_code, 400)

    def test_day_scale_saves_times_in_quarter_hours(self):
        task = make_task(self.user)
        self.session.post(f"/tasks/{task}/dates", {"start_at": "2026-11-26T14:15", "end_at": "2026-11-26T15:00"})
        row = task_row(task)
        self.assertEqual((row["start_date"], row["start_time"], row["due_date"], row["due_time"]),
                         ("2026-11-26", "14:15", "2026-11-26", "15:00"))
        # A whole day is stored as plain dates with no times.
        self.session.post(f"/tasks/{task}/dates", {"start_at": "2026-11-26T00:00", "end_at": "2026-11-27T00:00"})
        row = task_row(task)
        self.assertEqual((row["start_date"], row["start_time"], row["due_date"], row["due_time"]),
                         ("2026-11-26", None, "2026-11-26", None))


class GanttExtras(PageTest):
    @classmethod
    def setUpClass(cls):
        cls.user = make_user("manager")
        today = date.today()
        project = make_project(cls.user, name="Grouped Project")
        make_task(cls.user, "Grouped task", project, start_date=today.isoformat(), due_date=(today + timedelta(days=2)).isoformat())
        make_task(cls.user, "Loose task", start_date=today.isoformat(), due_date=today.isoformat())

    def test_group_by_project_adds_headings_and_can_be_turned_off(self):
        me = Session(self.user)
        for zoom in ("day", "month"):
            page = me.get(f"/tasks/gantt?zoom={zoom}&group=project").get_data(as_text=True)
            self.assertIn("tk-gantt-grouprow", page)
            self.assertIn("<strong>Grouped Project</strong>", page)
            self.assertIn("<strong>No project</strong>", page)
        self.assertNotIn("tk-gantt-grouprow", me.get("/tasks/gantt?group=none").get_data(as_text=True))

    def test_day_scale_can_be_paged_away_from_today(self):
        me = Session(self.user)
        far = (date.today() + timedelta(days=60)).isoformat()
        page = me.get(f"/tasks/gantt?zoom=day&start={far}&group=none").get_data(as_text=True)
        self.assertNotIn('id="gantt-today"', page)
        self.assertNotIn('class="tk-gantt-bar"', page)
        self.assertIn('id="gantt-today"', me.get("/tasks/gantt?zoom=day").get_data(as_text=True))


class Notifications(PageTest):
    @classmethod
    def setUpClass(cls):
        cls.manager, cls.staff = make_user("manager", "Notify Manager"), make_user(name="Notify Staff")

    def notes(self, user_id, kind):
        return query("SELECT * FROM notifications WHERE user_id=? AND kind=?", (user_id, kind))

    def test_comment_notifies_the_others_on_the_task(self):
        task = make_task(self.manager, "Discuss scope", assignees=[self.staff])
        Session(self.manager).post(f"/tasks/{task}/comment", {"body": "Please check the brief"})
        self.assertEqual(len(self.notes(self.staff, "task_comment")), 1)
        self.assertFalse(self.notes(self.manager, "task_comment"))   # not for your own comment

    def test_due_reminders_fire_once(self):
        today = datetime.now(crm.APP_TIMEZONE).date()
        person = make_user(name="Due Person")
        make_task(self.manager, "Due tomorrow task", assignees=[person], due_date=(today + timedelta(days=1)).isoformat())
        make_task(self.manager, "Due today task", assignees=[person], due_date=today.isoformat())
        make_task(self.manager, "Just overdue task", assignees=[person], due_date=(today - timedelta(days=1)).isoformat())
        make_task(self.manager, "Ancient task", assignees=[person], due_date=(today - timedelta(days=40)).isoformat())
        make_task(self.manager, "Finished task", assignees=[person], due_date=today.isoformat(), status="Complete")
        Session(person)
        Session(person)   # a second browser on the same day adds nothing
        kinds = sorted(row["kind"] for row in query("SELECT kind FROM notifications WHERE user_id=?", (person,)))
        self.assertEqual(kinds, ["task_due", "task_due_soon", "task_overdue"])


class Repeating(PageTest):
    def test_completing_a_weekly_task_opens_the_next_one_once(self):
        user, helper = make_user("manager"), make_user()
        me = Session(user)
        today = datetime.now(crm.APP_TIMEZONE).date()
        task = make_task(user, "Weekly report", assignees=[helper], start_date=(today - timedelta(days=1)).isoformat(),
                         due_date=today.isoformat(), estimate_minutes=60)
        # The task panel always posts every field, dates included.
        me.post(f"/tasks/{task}/update", {"title": "Weekly report", "status": "To Do", "repeat": "weekly", "estimate": "1h",
                                          "start_date": (today - timedelta(days=1)).isoformat(), "due_date": today.isoformat(),
                                          "assignees": [helper]})
        self.assertEqual(task_row(task)["repeat"], "weekly")
        me.post(f"/tasks/{task}/status", {"status": "Complete"})
        copies = query("SELECT * FROM work_tasks WHERE title='Weekly report' AND id!=?", (task,))
        self.assertEqual(len(copies), 1)
        new = copies[0]
        self.assertEqual((new["status"], new["repeat"], new["estimate_minutes"], new["start_date"], new["due_date"]),
                         ("To Do", "weekly", 60, (today + timedelta(days=6)).isoformat(), (today + timedelta(days=7)).isoformat()))
        self.assertEqual([r["user_id"] for r in query("SELECT user_id FROM work_task_assignees WHERE task_id=?", (new["id"],))], [helper])
        me.post(f"/tasks/{task}/status", {"status": "To Do"})
        me.post(f"/tasks/{task}/status", {"status": "Complete"})
        self.assertEqual(len(query("SELECT 1 FROM work_tasks WHERE title='Weekly report'")), 2)

    def test_monthly_repeat_handles_short_months(self):
        self.assertEqual(crm._next_repeat_date(date(2027, 1, 31), "monthly"), date(2027, 2, 28))
        self.assertEqual(crm._next_repeat_date(date(2026, 12, 15), "monthly"), date(2027, 1, 15))


class Links(PageTest):
    def test_links_are_added_from_their_own_field(self):
        user = make_user("manager")
        me, task = Session(user), make_task(user)
        me.post(f"/tasks/{task}/links", {"url": "example.com/page javascript:alert(1)"})
        self.assertEqual([row["url"] for row in query("SELECT url FROM work_task_links WHERE task_id=?", (task,))],
                         ["https://example.com/page"])
        me.post(f"/tasks/{task}/update", {"title": "T", "status": "To Do", "description": "see https://ignored.example.com"})
        self.assertEqual(len(query("SELECT 1 FROM work_task_links WHERE task_id=?", (task,))), 1)


if __name__ == "__main__":
    unittest.main()
