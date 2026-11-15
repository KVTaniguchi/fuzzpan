// Passkeys (decisions/0008). Loaded from /static (the CSP blocks inline scripts).
// Each page has a button with data-passkey="login" or "register" and a form with a
// hidden field. This script asks the device for the passkey, puts the result in the
// hidden field and submits the form as an ordinary POST, so the server's answer
// (redirect or page) is handled as for any form. Without JavaScript or passkey
// support the buttons stay hidden and password plus code still works.
(function () {
  if (!window.PublicKeyCredential || !navigator.credentials) return;

  function toBytes(b64url) {
    var b64 = b64url.replace(/-/g, "+").replace(/_/g, "/");
    while (b64.length % 4) b64 += "=";
    var raw = atob(b64), out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out.buffer;
  }
  function toB64url(buffer) {
    var bytes = new Uint8Array(buffer), raw = "";
    for (var i = 0; i < bytes.length; i++) raw += String.fromCharCode(bytes[i]);
    return btoa(raw).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function decodeOptions(o) {
    o.challenge = toBytes(o.challenge);
    if (o.user) o.user.id = toBytes(o.user.id);
    (o.excludeCredentials || []).concat(o.allowCredentials || []).forEach(function (c) {
      c.id = toBytes(c.id);
    });
    return o;
  }
  function encodeCredential(c) {
    var r = c.response, response = { clientDataJSON: toB64url(r.clientDataJSON) };
    if (r.attestationObject) {
      response.attestationObject = toB64url(r.attestationObject);
      if (r.getTransports) response.transports = r.getTransports();
    } else {
      response.authenticatorData = toB64url(r.authenticatorData);
      response.signature = toB64url(r.signature);
      if (r.userHandle) response.userHandle = toB64url(r.userHandle);
    }
    return JSON.stringify({
      id: c.id, rawId: toB64url(c.rawId), type: c.type, response: response,
      clientExtensionResults: c.getClientExtensionResults ? c.getClientExtensionResults() : {},
      authenticatorAttachment: c.authenticatorAttachment || undefined
    });
  }
  function say(button, text) {
    var status = document.getElementById(button.getAttribute("data-status"));
    if (status) status.textContent = text;
  }
  function submitWith(button, credential) {
    var form = document.getElementById(button.getAttribute("data-form"));
    form.querySelector("input[name='" + button.getAttribute("data-field") + "']").value =
      encodeCredential(credential);
    form.submit();  // not requestSubmit: the password fields are not needed now
  }
  function failed(button) {
    return function () {
      button.disabled = false;
      say(button, "No passkey was used. Try again, or use your password and code.");
    };
  }

  document.querySelectorAll("[data-passkey]").forEach(function (button) {
    button.hidden = false;
    button.addEventListener("click", function (event) {
      event.preventDefault();
      button.disabled = true;
      say(button, "");
      if (button.getAttribute("data-passkey") === "register") {
        var options = decodeOptions(JSON.parse(button.getAttribute("data-options")));
        navigator.credentials.create({ publicKey: options }).then(function (c) {
          submitWith(button, c);
        }, failed(button));
        return;
      }
      fetch("/passkey/options", { method: "POST", credentials: "same-origin" })
        .then(function (r) {
          if (!r.ok) throw new Error("options");
          return r.json();
        })
        .then(function (o) {
          return navigator.credentials.get({ publicKey: decodeOptions(o) });
        })
        .then(function (c) { submitWith(button, c); }, failed(button));
    });
  });
})();
