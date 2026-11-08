"""The existing job record exposes a human form only live, never during replay."""
from __future__ import annotations

import os
from pathlib import Path

from qumus2.ui.app import create_app
from tests.test_job_human_review import held_job
from tests.test_ui_browser import _page, _Server, browser  # noqa: F401


def test_job_hold_form_posts_current_failure_and_replay_has_no_action(services, browser):  # noqa: F811
    rid, failure = held_job(services)
    cursor = services.store.last_event_id()
    with _Server(create_app(services, start_driver=False)) as base:
        context, page, errors = _page(browser)
        try:
            page.goto(base)
            page.wait_for_selector('#panel')
            page.evaluate("""async ({id}) => {
              const {Results} = await import('/static/results.js');
              const {Session} = await import('/static/session.js');
              const {api, post} = await import('/static/ui.js');
              const held = await api('/api/runs/' + id);
              const node = document.createElement('div');
              node.id = 'review-job-fixture';
              node.style = 'position:fixed;inset:20px;background:var(--panel);z-index:100000;padding:20px;overflow:auto';
              document.body.append(node);
              window.reviewFixture = new Results(new Session(), {
                live: () => true, run: job => api('/api/runs/' + job), photo: () => {},
                humanReview: (job, body) => post('/api/runs/' + job + '/human-review', body)
              });
              window.reviewFixture.jobNode = node;
              window.reviewHeldPacket = held;
              await window.reviewFixture.buildJob(id, node);
            }""", {"id": rid})
            form = page.get_by_role('form', name='Review job hold')
            assert form.count() == 1
            form.get_by_role('button', name='Record human review').click()
            assert services.store.get_run(rid)["status"] == "waiting_human"
            form.get_by_label('Reviewer', exact=True).fill('Browser fixture operator')
            form.get_by_label('Review reason', exact=True).fill('Inspected this synthetic failure; normal recovery must recheck it')
            output = os.environ.get('QUMUS_HUMAN_REVIEW_SCREENSHOT_DIR')
            if output:
                Path(output).mkdir(parents=True, exist_ok=True)
                page.locator('#review-job-fixture').screenshot(path=str(Path(output) / 'job-human-review-form.png'))
            form.get_by_role('button', name='Record human review').click()
            page.wait_for_function("document.querySelectorAll('form[aria-label=\"Review job hold\"]').length === 0")
            assert services.store.get_run(rid)["status"] == "parked"
            receipt = next(e for e in services.store.events_after(cursor) if e.kind == "job.human_reviewed")
            assert receipt.payload["failure_event_id"] == failure
            assert receipt.payload["reviewer"] == 'Browser fixture operator'
            page.evaluate("""async () => {
              window.reviewFixture.hooks.live = () => false;
              window.reviewFixture.hooks.run = async () => window.reviewHeldPacket;
              await window.reviewFixture.buildJob(window.reviewHeldPacket.id, window.reviewFixture.jobNode);
            }""")
            assert page.get_by_role('form', name='Review job hold').count() == 0
            assert not any(e.kind == "device.command" or e.kind.startswith("agent.request")
                           for e in services.store.events_after(cursor))
            assert errors == []
        finally:
            context.close()
