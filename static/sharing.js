const element = id => document.getElementById(id);
const statuses = {active: '已登录', pending: '等待接受', expired: '需要新链接', revoked: '已撤销'};
let csrf = '';
let shownInvite = '';

async function send(path, method = 'GET', body) {
  const response = await fetch(path, {
    method,
    headers: method === 'GET' ? {} : {'X-CSRF-Token': csrf, 'Content-Type': 'application/json'},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (response.status === 401) {
    location.replace('/login');
    throw new Error('会话已过期');
  }
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '操作失败');
  return data;
}

function action(label, run, style = 'ghost') {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'btn sm ' + style;
  button.textContent = label;
  button.addEventListener('click', async () => {
    button.disabled = true;
    element('error').textContent = '';
    try {
      await run();
    } catch (error) {
      element('error').textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

function showInvite(link) {
  shownInvite = link;
  element('inviteLink').value = link;
  element('freshInvite').hidden = false;
  element('notice').textContent = '';
}

async function loadAccounts() {
  const {items} = await send('/api/cloud/invites');
  const list = element('accounts');
  list.replaceChildren();
  if (!items.length) list.textContent = '暂无邀请。';
  for (const item of items) {
    const row = document.createElement('div');
    row.className = 'share-row';
    const label = document.createElement('div');
    const name = document.createElement('strong');
    name.textContent = item.name;
    const status = document.createElement('span');
    status.textContent = statuses[item.status] || '只读';
    label.append(name, status);
    row.append(label);
    if (item.status !== 'revoked') {
      const buttons = document.createElement('div');
      const path = '/api/cloud/invites/' + encodeURIComponent(item.username);
      buttons.append(action('生成新链接', async () => {
        const result = await send(path + '/renew', 'POST');
        showInvite(result.link);
        await loadAccounts();
      }), action('撤销访问', async () => {
        if (!window.confirm('撤销后，该同事已登录的浏览器也会失去访问权限。')) return;
        await send(path, 'DELETE');
        shownInvite = '';
        element('inviteLink').value = '';
        element('freshInvite').hidden = true;
        element('notice').textContent = '访问已撤销。';
        await loadAccounts();
      }, 'danger'));
      row.append(buttons);
    }
    list.append(row);
  }
}

element('inviteForm').addEventListener('submit', async event => {
  event.preventDefault();
  const button = element('submitInvite');
  button.disabled = true;
  element('error').textContent = '';
  try {
    const result = await send('/api/cloud/invites', 'POST', {name: element('person').value});
    showInvite(result.link);
    element('person').value = '';
    await loadAccounts();
  } catch (error) {
    element('error').textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

element('copyInvite').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(shownInvite);
    element('notice').textContent = '链接已复制。';
  } catch {
    element('inviteLink').select();
    element('notice').textContent = '请按 Ctrl+C 复制。';
  }
});

send('/api/me').then(user => {
  csrf = user.csrf;
  element('submitInvite').disabled = !user.cloudSharing;
  return loadAccounts();
}).catch(error => { element('error').textContent = error.message; });
