import assert from 'node:assert/strict';
import { test } from 'node:test';
import { meterText, meterTitle, tokens } from '../qumus2/ui/static/contextmeter.js';

const live = {supported: true, mode: 'sdk', model: 'openai/gpt-5.5', auto: true, window_percent: 50,
  window_tokens: 1000000, threshold_tokens: 500000, input_tokens: 82000, estimated: false, compactions: 0};

test('meter reads the measured size and the automatic point', () => {
  assert.equal(tokens(82000), '82k');
  assert.equal(tokens(1050000), '1.1M');
  assert.equal(meterText(live), 'Context 82k / auto 500k');
  assert.equal(meterText({...live, input_tokens: 38000, estimated: true}), 'Context ≈38k / auto 500k');
  assert.equal(meterText({...live, input_tokens: null}), 'Context not measured yet / auto 500k');
  assert.equal(meterText({...live, auto: false}), 'Context 82k / auto off');
  assert.equal(meterText({supported: false}), null);
});

test('meter title explains the window share and the archive', () => {
  const title = meterTitle({...live, compactions: 2});
  assert.match(title, /50% \(500,000 tokens; 1M token window of openai\/gpt-5.5\)/);
  assert.match(title, /Compacted 2 times; the full history stays/);
  assert.match(meterTitle({...live, window_tokens: null, threshold_tokens: 150000}), /window unknown/);
  assert.match(meterTitle({...live, auto: false, auto_disabled_by_env: true}), /QUMUS_COMPACTION=off/);
});
