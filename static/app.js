const optionsEl = document.querySelector('#options');
const template = document.querySelector('#optionTemplate');
const statusEl = document.querySelector('#status');
const resultsEl = document.querySelector('#results');
const summaryEl = document.querySelector('#summary');
const methodNoteEl = document.querySelector('#methodNote');
const analyzeButton = document.querySelector('#analyze');

const map = L.map('map').setView([31.2304, 121.4737], 11);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap contributors'
}).addTo(map);
let markers = [];

function addOption(data = {}) {
  const node = template.content.cloneNode(true);
  const card = node.querySelector('.option-card');
  card.querySelector('.name').value = data.name || '';
  card.querySelector('.address').value = data.address || '';
  card.querySelector('.rent').value = data.rent || '';
  card.querySelector('.environment').value = data.environment || 'verify';
  card.querySelector('.amenities').value = data.amenities || 'verify';
  card.querySelector('.notes').value = data.notes || '';
  card.querySelector('.remove').addEventListener('click', () => card.remove());
  optionsEl.appendChild(node);
}

addOption({
  name: '一号房',
  address: '上海市静安区南京西路',
  rent: 6200,
  environment: 'satisfied',
  amenities: 'convenient'
});
addOption({
  name: '二号房',
  address: '上海市普陀区曹杨路',
  rent: 4900,
  environment: 'acceptable',
  amenities: 'average'
});
addOption({
  name: '三号房',
  address: '上海市虹口区四川北路',
  rent: 4500,
  environment: 'verify',
  amenities: 'convenient'
});

document.querySelector('#addOption').addEventListener('click', () => addOption());

analyzeButton.addEventListener('click', async () => {
  const cards = [...document.querySelectorAll('.option-card')];
  if (!cards.length) {
    showStatus('至少添加一个候选房源。', true);
    return;
  }

  const payload = {
    workplace: document.querySelector('#workplace').value.trim(),
    budget: Number(document.querySelector('#budget').value),
    max_commute: Number(document.querySelector('#maxCommute').value),
    priority: document.querySelector('#priority').value,
    options: cards.map(card => ({
      name: card.querySelector('.name').value.trim(),
      address: card.querySelector('.address').value.trim(),
      rent: Number(card.querySelector('.rent').value),
      environment: card.querySelector('.environment').value,
      amenities: card.querySelector('.amenities').value,
      notes: card.querySelector('.notes').value.trim()
    }))
  };

  const validationError = validatePayload(payload);
  if (validationError) {
    showStatus(validationError, true);
    return;
  }

  analyzeButton.disabled = true;
  showStatus('正在解析地址并计算驾车通勤时间…');
  resultsEl.innerHTML = '';
  methodNoteEl.textContent = '';

  try {
    const response = await fetch('/api/analyze', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });

    const text = await response.text();
    let data = null;
    if (text.trim()) {
      try {
        data = JSON.parse(text);
      } catch (parseError) {
        throw new Error(`服务器返回了无法识别的内容（HTTP ${response.status}）。请查看 Render 日志。`);
      }
    }

    if (!response.ok) {
      throw new Error(data?.error || `分析失败（HTTP ${response.status}）`);
    }
    if (!data || !Array.isArray(data.ranked)) {
      throw new Error('服务器没有返回完整的分析结果。');
    }

    renderResults(data);
    showStatus(data.ai_warning || '分析完成。', Boolean(data.ai_warning));
  } catch (error) {
    showStatus(error?.message || '分析失败，请稍后重试。', true);
  } finally {
    analyzeButton.disabled = false;
  }
});

function validatePayload(payload) {
  if (!payload.workplace) return '请填写工作地点。';
  if (!Number.isFinite(payload.budget) || payload.budget <= 0) return '请填写有效的月租预算。';
  if (!Number.isFinite(payload.max_commute) || payload.max_commute <= 0) return '请填写有效的通勤时间。';
  for (let index = 0; index < payload.options.length; index += 1) {
    const option = payload.options[index];
    if (!option.address) return `请填写第 ${index + 1} 个候选房源的地址。`;
    if (!Number.isFinite(option.rent) || option.rent <= 0) return `请填写第 ${index + 1} 个候选房源的有效月租。`;
  }
  return '';
}

function showStatus(message, isError = false) {
  statusEl.textContent = message;
  statusEl.classList.toggle('error', isError);
}

function renderResults(data) {
  markers.forEach(marker => marker.remove());
  markers = [];
  const bounds = [];

  const workMarker = L.marker([data.workplace.lat, data.workplace.lon])
    .addTo(map)
    .bindPopup('工作地点');
  markers.push(workMarker);
  bounds.push([data.workplace.lat, data.workplace.lon]);

  data.ranked.forEach((item, index) => {
    const marker = L.marker([item.lat, item.lon])
      .addTo(map)
      .bindPopup(`${index + 1}. ${escapeHtml(item.name)}<br>${escapeHtml(item.decision)}`);
    markers.push(marker);
    bounds.push([item.lat, item.lon]);
  });

  if (bounds.length > 1) {
    map.fitBounds(bounds, {padding: [40, 40]});
  } else {
    map.setView(bounds[0], 13);
  }

  summaryEl.classList.remove('empty');
  summaryEl.textContent = data.summary;
  methodNoteEl.textContent = `${data.method_note} 当前侧重：${data.priority_label}。`;

  resultsEl.innerHTML = data.ranked.map((item, index) => `
    <article class="result-card">
      <div class="rank">${index + 1}</div>
      <div class="result-main">
        <div class="result-title-row">
          <h3>${escapeHtml(item.name)}</h3>
          <span class="decision decision-${item.decision_tier}">${escapeHtml(item.decision)}</span>
        </div>
        <div class="meta">
          ${escapeHtml(item.address)}<br>
          月租 ¥${formatNumber(item.rent)} · 驾车通勤约 ${item.duration_min} 分钟 / ${item.distance_km} km<br>
          居住环境：${escapeHtml(item.environment_label)} · 生活配套：${escapeHtml(item.amenities_label)}
          ${item.notes ? `<br>备注：${escapeHtml(item.notes)}` : ''}
        </div>
        <ul class="reasons">
          ${item.reasons.map(reason => `<li>${escapeHtml(reason)}</li>`).join('')}
        </ul>
      </div>
    </article>
  `).join('');
}

function formatNumber(value) {
  return new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 0}).format(value);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}
