/*
 * EDP 在线客服嵌入脚本。在网站页面中加入：
 *
 *   <script src="https://<widget 地址>/embed.js" data-key="<渠道 key>" async></script>
 *
 * 实名访客（可选）：网站后端用渠道的签名密钥为当前登录用户签名后，在加载脚本前设置
 *
 *   window.EDPWidgetConfig = {
 *     user: { external_id: '10086', name: '王先生', timestamp: 1790000000, signature: '<hex>' },
 *   }
 *
 * signature = HMAC-SHA256(签名密钥, `${external_id}:${name}:${timestamp}`)（name 为空时写空字符串）。
 */
;(function () {
  var script = document.currentScript
  if (!script || window.__edpWidgetLoaded) return
  window.__edpWidgetLoaded = true

  var key = script.getAttribute('data-key')
  if (!key) {
    console.warn('[EDP] embed.js 缺少 data-key')
    return
  }
  var base = new URL(script.src, location.href).origin
  var config = window.EDPWidgetConfig || {}
  var params = new URLSearchParams({
    key: key,
    embed: '1',
    page_url: location.href,
    referrer: document.referrer,
  })
  // 身份放在 URL 的 # 之后：不会发给任何服务器，也不会出现在访问日志里。
  var hash = config.user ? '#identity=' + encodeURIComponent(JSON.stringify(config.user)) : ''

  var root = document.createElement('div')
  root.setAttribute('data-edp-widget', '')
  root.style.cssText = 'position:fixed;right:20px;bottom:20px;z-index:2147483000;font-family:sans-serif'

  var frame = document.createElement('iframe')
  frame.title = '在线客服'
  frame.src = base + '/?' + params.toString() + hash
  frame.style.cssText =
    'display:none;width:380px;height:600px;max-width:calc(100vw - 40px);max-height:calc(100vh - 100px);' +
    'border:0;border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.18);margin-bottom:12px;background:#fff'

  var button = document.createElement('button')
  button.type = 'button'
  button.textContent = '在线客服'
  button.setAttribute('data-edp-widget-button', '')
  button.style.cssText =
    'display:block;margin-left:auto;padding:12px 20px;border:0;border-radius:24px;cursor:pointer;' +
    'background:#1677ff;color:#fff;font-size:15px;box-shadow:0 4px 14px rgba(22,119,255,.4);position:relative'

  var badge = document.createElement('span')
  badge.style.cssText =
    'display:none;position:absolute;top:-6px;right:-6px;min-width:18px;height:18px;padding:0 5px;' +
    'border-radius:9px;background:#ff4d4f;color:#fff;font-size:12px;line-height:18px;text-align:center'
  button.appendChild(badge)

  var open = false
  function post(type) {
    if (frame.contentWindow) frame.contentWindow.postMessage({ type: type }, base)
  }
  function setOpen(value) {
    open = value
    frame.style.display = open ? 'block' : 'none'
    button.firstChild.nodeValue = open ? '收起' : '在线客服'
    if (open) badge.style.display = 'none'
    post(open ? 'edp:open' : 'edp:hidden')
  }
  button.addEventListener('click', function () {
    setOpen(!open)
  })
  window.addEventListener('message', function (event) {
    if (event.origin !== base || !event.data) return
    if (event.data.type === 'edp:unread' && !open) {
      var count = Number(event.data.count) || 0
      badge.textContent = count > 99 ? '99+' : String(count)
      badge.style.display = count > 0 ? 'block' : 'none'
    }
    if (event.data.type === 'edp:close') setOpen(false)
  })

  root.appendChild(frame)
  root.appendChild(button)
  function mount() {
    document.body.appendChild(root)
  }
  if (document.body) mount()
  else document.addEventListener('DOMContentLoaded', mount)
})()
