// @ts-check
/**
 * Smart Shopper — Chat Widget E2E Tests
 *
 * Tests the chatbot UI: open/close, sending messages,
 * keyboard interactions, and API endpoints.
 *
 * Usage: node tests/playwright/test_chat_widget.js
 */

const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

const BASE = 'http://127.0.0.1:8080';
const SCREENSHOT_DIR = path.join(__dirname, 'screenshots');
const PAUSE_MS = 600;

let passed = 0;
let failed = 0;
const failures = [];

function assert(condition, msg) {
  if (condition) {
    passed++;
    console.log('  PASS: ' + msg);
  } else {
    failed++;
    failures.push(msg);
    console.log('  FAIL: ' + msg);
  }
}

async function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

(async () => {
  console.log('\n=== Smart Shopper Chat Widget E2E Tests ===\n');

  if (!fs.existsSync(SCREENSHOT_DIR)) {
    fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  }

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
  });
  const page = await context.newPage();

  try {
    // -- Test 1: Chat fab visible on page load
    console.log('\n[1] Chat fab visibility');
    await page.goto(BASE + '/dashboard', { waitUntil: 'networkidle' });
    await sleep(PAUSE_MS);

    const fab = await page.$('.chat-fab');
    assert(fab !== null, 'Chat fab button exists on dashboard');

    const fabVisible = fab ? await fab.isVisible() : false;
    assert(fabVisible, 'Chat fab is visible');

    await page.screenshot({
      path: path.join(SCREENSHOT_DIR, 'chat_01_fab_visible.png'),
    });

    // -- Test 2: Click fab opens panel
    console.log('\n[2] Open chat panel');
    if (fab) await fab.click();
    await sleep(PAUSE_MS);

    const panelVisible = await page.isVisible('.chat-panel');
    assert(panelVisible, 'Chat panel is visible after clicking fab');

    const headerTitle = await page.textContent('.chat-header-title');
    assert(
      headerTitle && headerTitle.trim() === 'Smart Shopper Chat',
      'Header title is "Smart Shopper Chat"'
    );

    await page.screenshot({
      path: path.join(SCREENSHOT_DIR, 'chat_02_panel_open.png'),
    });

    // -- Test 3: Welcome screen
    console.log('\n[3] Welcome screen');
    const welcomeTitle = await page.textContent('.chat-welcome-title');
    assert(
      welcomeTitle && welcomeTitle.trim() === 'Your Personal Chef',
      'Welcome title shows "Your Personal Chef"'
    );

    const chips = await page.$$('.chat-chip');
    assert(chips.length === 3, 'Three quick-action chips present (got ' + chips.length + ')');

    // -- Test 4: Close button
    console.log('\n[4] Close button');
    await page.click('.chat-close-btn');
    await sleep(PAUSE_MS);

    const panelAfterClose = await page.isVisible('.chat-panel');
    assert(!panelAfterClose, 'Panel hidden after clicking close');

    // -- Test 5: Escape key closes
    console.log('\n[5] Escape key');
    await page.click('.chat-fab');
    await sleep(PAUSE_MS);
    assert(await page.isVisible('.chat-panel'), 'Panel re-opened');

    await page.keyboard.press('Escape');
    await sleep(PAUSE_MS);
    assert(!(await page.isVisible('.chat-panel')), 'Panel closed via Escape');

    // -- Test 6: Input field and send button
    console.log('\n[6] Input field');
    await page.click('.chat-fab');
    await sleep(PAUSE_MS);

    const sendBtn = await page.$('.chat-send-btn');
    const sendDisabledEmpty = sendBtn ? await sendBtn.isDisabled() : true;
    assert(sendDisabledEmpty, 'Send button disabled when input empty');

    await page.fill('.chat-input', 'Hello');
    await sleep(200);

    const sendDisabledFilled = sendBtn ? await sendBtn.isDisabled() : true;
    assert(!sendDisabledFilled, 'Send button enabled after typing');

    // -- Test 7: Send a message
    console.log('\n[7] Send message');
    await page.fill('.chat-input', 'Hello there');
    await page.keyboard.press('Enter');
    await sleep(PAUSE_MS);

    const userBubbles = await page.$$('.chat-bubble-user');
    assert(userBubbles.length >= 1, 'User message bubble appears');

    if (userBubbles.length > 0) {
      const userText = await userBubbles[0].textContent();
      assert(
        userText && userText.includes('Hello there'),
        'User bubble contains "Hello there"'
      );
    }

    await page.screenshot({
      path: path.join(SCREENSHOT_DIR, 'chat_03_message_sent.png'),
    });

    // Wait for response (DeepSeek or error)
    await sleep(10000);

    const assistantBubbles = await page.$$('.chat-bubble-assistant');
    assert(
      assistantBubbles.length >= 1,
      'Assistant response received (' + assistantBubbles.length + ' bubble(s))'
    );

    await page.screenshot({
      path: path.join(SCREENSHOT_DIR, 'chat_04_response_received.png'),
    });

    // -- Test 8: Chat appears on other pages
    console.log('\n[8] Chat on multiple pages');
    await page.click('.chat-close-btn').catch(() => {});
    await sleep(300);

    for (const route of ['/recipes', '/pantry', '/products']) {
      await page.goto(BASE + route, { waitUntil: 'networkidle' });
      await sleep(500);
      const fabOnPage = await page.$('.chat-fab');
      assert(fabOnPage !== null, 'Chat fab exists on ' + route);
    }

    // -- Test 9: API endpoint — message
    console.log('\n[9] API endpoints');
    const msgResp = await page.request.post(BASE + '/api/chat/message', {
      data: { messages: [], user_message: 'hello' },
    });
    assert(msgResp.status() === 200, 'POST /api/chat/message returns 200');

    const msgData = await msgResp.json();
    assert(
      !!(msgData.response || msgData.error),
      'Message response has content'
    );

    // -- Test 10: API endpoint — approve
    const approveResp = await page.request.post(BASE + '/api/chat/approve', {
      data: { id: 'test', function_name: 'clear_cart', args: {} },
    });
    assert(approveResp.status() === 200, 'POST /api/chat/approve returns 200');
    const approveData = await approveResp.json();
    assert(approveData.success === true, 'Approve returns success: true');

    // -- Test 11: API endpoint — reject
    const rejectResp = await page.request.post(BASE + '/api/chat/reject', {
      data: { id: 'test' },
    });
    assert(rejectResp.status() === 200, 'POST /api/chat/reject returns 200');

    // -- Test 12: API endpoint — empty message rejected
    const emptyResp = await page.request.post(BASE + '/api/chat/message', {
      data: { messages: [], user_message: '   ' },
    });
    assert(emptyResp.status() === 400, 'Empty message returns 400');

  } catch (err) {
    console.error('\nFATAL ERROR:', err.message);
    failed++;
    failures.push('Fatal: ' + err.message);
  } finally {
    await browser.close();
  }

  // -- Summary
  console.log('\n' + '='.repeat(50));
  console.log('Results: ' + passed + ' passed, ' + failed + ' failed');
  if (failures.length > 0) {
    console.log('\nFailures:');
    failures.forEach(function(f) { console.log('  - ' + f); });
  }
  console.log('='.repeat(50) + '\n');

  process.exit(failed > 0 ? 1 : 0);
})();
