/* global $, bootstrap */
$(function () {
  "use strict";

  // ── State ────────────────────────────────────────────────────────────────
  var conversationHistory = [];
  var totalInputTokens  = 0;
  var totalOutputTokens = 0;

  // ── DOM refs ─────────────────────────────────────────────────────────────
  var $chatArea     = $("#chat-area");
  var $welcomeCard  = $("#welcome-card");
  var $form         = $("#chat-form");
  var $input        = $("#msg-input");
  var $btnSend      = $("#btn-send");
  var $btnNew       = $("#btn-new-chat");
  var $sysProm      = $("#system-prompt");
  var $healthDot    = $("#health-dot");
  var $healthText   = $("#health-text");
  var $errorAlert   = $("#error-alert");
  var $errorText    = $("#error-text");
  var $modelBadge   = $("#model-badge");
  var $usageBadge   = $("#usage-badge");
  var $infoMsgCount = $("#info-msg-count");
  var $infoTokens   = $("#info-tokens");
  var $infoFlow     = $("#info-flow");
  var $sidebar      = $("#sidebar");
  var $overlay      = $("#sidebar-overlay");
  var $hamburger    = $("#hamburger");

  // ── Helpers ──────────────────────────────────────────────────────────────
  function timestamp() {
    return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function scrollToBottom() {
    $chatArea.scrollTop($chatArea[0].scrollHeight);
  }

  function escapeHtml(str) {
    return $("<span>").text(str).html();
  }

  function updateSessionInfo() {
    $infoMsgCount.text(conversationHistory.length);
    if (totalInputTokens || totalOutputTokens) {
      $infoTokens.text((totalInputTokens + totalOutputTokens).toLocaleString());
      $usageBadge.text("↑" + totalInputTokens.toLocaleString() + " ↓" + totalOutputTokens.toLocaleString());
    }
  }

  // ── Bubbles ──────────────────────────────────────────────────────────────
  function appendBubble(role, text) {
    $welcomeCard.hide();

    var isUser = role === "user";
    var icon   = isUser ? "bi-person-fill" : "bi-robot";
    var rowCls = "msg-row " + role;

    var html =
      '<div class="' + rowCls + '">' +
        '<div class="msg-avatar"><i class="bi ' + icon + '"></i></div>' +
        '<div>' +
          '<div class="msg-bubble">' + escapeHtml(text) + '</div>' +
          '<div class="msg-meta">' + timestamp() + '</div>' +
        '</div>' +
      '</div>';

    $chatArea.append(html);
    scrollToBottom();
  }

  function showTyping() {
    var html =
      '<div id="typing-indicator" class="msg-row assistant typing-indicator">' +
        '<div class="msg-avatar"><i class="bi bi-robot"></i></div>' +
        '<div class="msg-bubble">' +
          '<span class="typing-dot"></span>' +
          '<span class="typing-dot"></span>' +
          '<span class="typing-dot"></span>' +
        '</div>' +
      '</div>';
    $chatArea.append(html);
    scrollToBottom();
  }

  function hideTyping() {
    $("#typing-indicator").remove();
  }

  function showError(msg) {
    $errorText.text(msg);
    $errorAlert.removeClass("d-none");
  }

  function setInputEnabled(enabled) {
    $input.prop("disabled", !enabled);
    $btnSend.prop("disabled", !enabled);
    if (enabled) { $input.focus(); }
  }

  // ── Health check ─────────────────────────────────────────────────────────
  function checkHealth() {
    $healthDot.attr("class", "health-dot health-check");
    $healthText.text("checking\u2026");

    $.ajax({
      url: "/health",
      method: "GET",
      timeout: 5000,
      success: function (data) {
        $healthDot.attr("class", "health-dot health-ok");
        $healthText.text(data.status || "ok");
      },
      error: function () {
        $healthDot.attr("class", "health-dot health-err");
        $healthText.text("unreachable");
      }
    });
  }

  checkHealth();
  setInterval(checkHealth, 30000);

  // ── Send message ─────────────────────────────────────────────────────────
  function sendMessage() {
    var text = $input.val().trim();
    if (!text) return;

    conversationHistory.push({ role: "user", content: text });
    appendBubble("user", text);
    updateSessionInfo();

    $input.val("");
    $input.css("height", "auto");
    $errorAlert.addClass("d-none");
    setInputEnabled(false);
    showTyping();

    var body = { messages: conversationHistory };
    var sys  = $sysProm.val().trim();
    if (sys) { body.system = sys; }

    $.ajax({
      url: "/chat",
      method: "POST",
      contentType: "application/json",
      data: JSON.stringify(body),
      timeout: 120000,
      success: function (data) {
        hideTyping();
        var reply = data.response || "(empty response)";
        conversationHistory.push({ role: "assistant", content: reply });
        appendBubble("assistant", reply);

        if (data.model) { $modelBadge.text(data.model); }
        if (data.flow)  { $infoFlow.text(data.flow); }
        if (data.usage) {
          totalInputTokens  += data.usage.input_tokens  || 0;
          totalOutputTokens += data.usage.output_tokens || 0;
        }
        updateSessionInfo();
        setInputEnabled(true);
      },
      error: function (xhr) {
        hideTyping();
        var detail = "";
        try {
          var json = JSON.parse(xhr.responseText);
          detail = json.error || json.details || xhr.responseText;
        } catch (e) {
          detail = xhr.statusText || "Unknown error";
        }
        showError("Error " + (xhr.status || "") + ": " + detail);
        conversationHistory.pop();
        updateSessionInfo();
        setInputEnabled(true);
      }
    });
  }

  // ── Events ───────────────────────────────────────────────────────────────
  $form.on("submit", function (e) {
    e.preventDefault();
    sendMessage();
  });

  $input.on("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  // Auto-resize textarea
  $input.on("input", function () {
    this.style.height = "auto";
    this.style.height = this.scrollHeight + "px";
  });

  // New chat
  $btnNew.on("click", function () {
    conversationHistory = [];
    totalInputTokens = 0;
    totalOutputTokens = 0;
    $chatArea.empty().append($welcomeCard.show());
    $errorAlert.addClass("d-none");
    $modelBadge.text("\u2014");
    $usageBadge.text("");
    $infoFlow.text("\u2014");
    $infoTokens.text("\u2014");
    $infoMsgCount.text("0");
    $input.val("").focus();
  });

  // Dismiss error on close
  $errorAlert.on("closed.bs.alert", function () {
    $errorAlert.addClass("d-none");
  });

  // ── Mobile sidebar toggle ────────────────────────────────────────────────
  $hamburger.on("click", function () {
    $sidebar.toggleClass("open");
    $overlay.toggleClass("show");
  });

  $overlay.on("click", function () {
    $sidebar.removeClass("open");
    $overlay.removeClass("show");
  });
});
