const { default: makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } = require('@whiskeysockets/baileys');
const qrcode = require('qrcode-terminal');
const qrcodePng = require('qrcode');
const { Boom } = require('@hapi/boom');
const pino = require('pino');
const express = require('express');
const fs = require('fs');
const path = require('path');

const PORT = 3001;
const INCOMING_LOG_PATH = path.join(__dirname, 'incoming_messages.json');

let sock = null;
let authState = null;
let connected = false;
let latestQr = null;

const LABELS_PATH = path.join(__dirname, 'labels.json');
let labels = {};
try { labels = JSON.parse(fs.readFileSync(LABELS_PATH, 'utf8')); } catch (_) { labels = {}; }
function saveLabels() {
    try { fs.writeFileSync(LABELS_PATH, JSON.stringify(labels, null, 2)); } catch (_) {}
}

function appendIncomingMessage(entry) {
    let log = [];
    if (fs.existsSync(INCOMING_LOG_PATH)) {
        try { log = JSON.parse(fs.readFileSync(INCOMING_LOG_PATH, 'utf8')); } catch (_) { log = []; }
    }
    log.push(entry);
    fs.writeFileSync(INCOMING_LOG_PATH, JSON.stringify(log, null, 2));
}

async function start() {
    const { state, saveCreds } = await useMultiFileAuthState('auth_info');
    authState = state;

    const { version } = await fetchLatestBaileysVersion();

    sock = makeWASocket({
        version,
        auth: state,
        logger: pino({ level: 'silent' }),
    });

    sock.ev.on('connection.update', (update) => {
        const { connection, lastDisconnect, qr } = update;

        if (qr) {
            latestQr = qr;
            console.log('Scan this QR code with WhatsApp:');
            qrcode.generate(qr, { small: true });
            console.log(`Or open http://localhost:${PORT}/qr in a browser.`);
        }

        if (connection === 'close') {
            connected = false;
            const statusCode = lastDisconnect?.error instanceof Boom
                ? lastDisconnect.error.output?.statusCode
                : undefined;
            const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
            console.log('Connection closed. Status code:', statusCode, 'Reason:', lastDisconnect?.error?.message);
            console.log('Reconnecting:', shouldReconnect);
            if (shouldReconnect) start();
        } else if (connection === 'open') {
            connected = true;
            latestQr = null;
            console.log('Connected to WhatsApp!');
            // labels only arrive via app-state sync; an old linked session never gets them, so pull them once
            if (!Object.keys(labels).length) {
                setTimeout(() => resyncLabels().catch((e) => console.log('Label resync failed:', e.message)), 5000);
            }
        }
    });

    sock.ev.on('creds.update', saveCreds);

    // WhatsApp Business labels arrive through app-state sync; keep name -> id for /label-chat
    sock.ev.on('labels.edit', (label) => {
        if (!label || !label.id) return;
        labels[label.id] = label;
        saveLabels();
    });

    sock.ev.on('messages.upsert', ({ messages }) => {
        for (const msg of messages) {
            if (msg.key.fromMe) continue;
            const text = msg.message?.conversation
                || msg.message?.extendedTextMessage?.text
                || null;
            appendIncomingMessage({
                from: msg.key.remoteJid,
                text,
                timestamp: Number(msg.messageTimestamp) * 1000,
                received_at: new Date().toISOString(),
            });
        }
    });
}

function phoneToJid(phone) {
    const digits = phone.replace(/[^\d]/g, '');
    return `${digits}@s.whatsapp.net`;
}

const app = express();
app.use(express.json());

app.get('/status', (req, res) => {
    res.json({ connected });
});

app.get('/qr', async (req, res) => {
    if (connected) {
        return res.send('<html><body style="font-family:sans-serif;text-align:center;margin-top:4em"><h2>Already connected to WhatsApp.</h2></body></html>');
    }
    if (!latestQr) {
        return res.send('<html><head><meta http-equiv="refresh" content="2"></head><body style="font-family:sans-serif;text-align:center;margin-top:4em"><h2>Waiting for QR code...</h2></body></html>');
    }
    const dataUrl = await qrcodePng.toDataURL(latestQr, { width: 320 });
    res.send(`<html><head><meta http-equiv="refresh" content="20"></head>
        <body style="font-family:sans-serif;text-align:center;margin-top:2em">
        <h2>Scan with WhatsApp &rarr; Linked Devices &rarr; Link a Device</h2>
        <img src="${dataUrl}" width="320" height="320" />
        <p>Page auto-refreshes every 20s with a fresh code.</p>
        </body></html>`);
});

app.get('/check-whatsapp', async (req, res) => {
    const phone = req.query.phone;
    if (!phone) return res.status(400).json({ error: 'phone query param required' });
    if (!connected) return res.status(503).json({ error: 'not connected to WhatsApp yet' });
    try {
        const jid = phoneToJid(phone);
        const digits = jid.split('@')[0];
        const [result] = await sock.onWhatsApp(digits);
        res.json({ phone, exists: !!(result && result.exists), jid: result ? result.jid : null });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

app.post('/send-message', async (req, res) => {
    const { phone, message } = req.body || {};
    if (!phone || !message) return res.status(400).json({ error: 'phone and message required' });
    if (!connected) return res.status(503).json({ error: 'not connected to WhatsApp yet' });
    try {
        const jid = phoneToJid(phone);
        await sock.sendMessage(jid, { text: message });
        res.json({ sent: true });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

async function resyncLabels() {
    // the saved 'regular' collection is already at the latest version, so the server would send nothing;
    // dropping the stored version makes it send the full snapshot again, including the label definitions
    await authState.keys.set({ 'app-state-sync-version': { regular: null } });
    await sock.resyncAppState(['regular', 'regular_low', 'regular_high', 'critical_block', 'critical_unblock_low'], true);
    console.log('Label resync done; labels known:', Object.keys(labels).length);
}

app.post('/resync-labels', async (req, res) => {
    if (!connected) return res.status(503).json({ error: 'not connected to WhatsApp yet' });
    try {
        await resyncLabels();
        res.json({ labels: Object.values(labels).filter((l) => !l.deleted) });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

app.get('/labels', (req, res) => {
    res.json({ labels: Object.values(labels).filter((l) => !l.deleted) });
});

app.post('/label-chat', async (req, res) => {
    const { phone, label } = req.body || {};
    if (!phone || !label) return res.status(400).json({ error: 'phone and label required' });
    if (!connected) return res.status(503).json({ error: 'not connected to WhatsApp yet' });
    const wanted = String(label).trim().toLowerCase();
    const found = Object.values(labels).find((l) => !l.deleted && String(l.name).trim().toLowerCase() === wanted);
    if (!found) {
        return res.status(404).json({
            error: `label '${label}' not known to the bridge (WhatsApp Business labels sync on connect; see GET /labels)`,
            known: Object.values(labels).filter((l) => !l.deleted).map((l) => l.name),
        });
    }
    try {
        await sock.addChatLabel(phoneToJid(phone), found.id);
        res.json({ labelled: true, label: found.name, labelId: found.id });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

app.post('/save-contact', async (req, res) => {
    const { phone, name } = req.body || {};
    if (!phone || !name) return res.status(400).json({ error: 'phone and name required' });
    if (!connected) return res.status(503).json({ error: 'not connected to WhatsApp yet' });
    try {
        await sock.addOrEditContact(phoneToJid(phone), {
            fullName: name,
            firstName: name,
            saveOnPrimaryAddressbook: false,
        });
        res.json({ saved: true });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

app.listen(PORT, () => console.log(`HTTP bridge listening on http://localhost:${PORT}`));

start();
