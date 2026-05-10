# Novaex AI Auto-Register Telegram Bot

Telegram bot yang membungkus skrip `novaex_register.py` menjadi pengalaman
berbasis tombol-menu, lengkap dengan progress real-time, tombol cancel,
manajemen proxy, dan export CSV. Siap di-deploy ke **Koyeb Free Web Service**.

## Fitur

- 🚀 **Buat Akun** dengan wizard (jumlah → thread → invite → GA → konfirmasi)
- ⏱ **Progress real-time** dengan progress bar dan tombol **Cancel & Simpan**
- 💾 Setiap akun yang sukses langsung di-append ke `accounts.csv`, jadi cancel
  di tengah jalan **tidak menghapus** akun yang sudah dibuat
- 📤 **Export Akun** sebagai dokumen CSV langsung di Telegram
- 🌐 **Kelola Proxy**: upload `.txt`, lihat jumlah baris, hapus
- 🔐 Mode private opsional (`ADMIN_USER_IDS`)
- ⚙️ FastAPI HTTP endpoint (`/api/health`, `/api/stats`) supaya cocok
  dengan Koyeb Web Service health check

## Struktur

```
backend/
  server.py       # FastAPI + bot lifespan
  bot.py          # Telegram handlers, menu, wizard, progress UI
  registrar.py    # Refactored Novaex registration core
frontend/         # Landing page (React)
Dockerfile        # Image untuk Koyeb
koyeb.yaml        # Manifest deploy
```

## Environment variables

| Variable              | Default        | Keterangan                                  |
| --------------------- | -------------- | ------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`  | —              | **Wajib**, dari @BotFather                  |
| `ADMIN_USER_IDS`      | (kosong)       | Daftar Telegram user ID dipisahkan koma. Kosongkan untuk publik. |
| `DEFAULT_INVITE_CODE` | `3tb84z`       | Invite code fallback                        |
| `DATA_DIR`            | `./data`       | Lokasi proxies.txt + accounts.csv           |
| `PORT`                | `8000`         | Port HTTP untuk Koyeb                       |

## Cara pakai bot

1. Kirim `/start` ke bot Anda di Telegram.
2. Tekan **🌐 Kelola Proxy → ⬆️ Upload proxy.txt**, lalu kirim file `.txt`
   (1 proxy per baris, format `user:pass@host:port`).  
   *Boleh dilewati — bot akan jalan tanpa proxy.*
3. Tekan **🚀 Buat Akun** → ikuti wizard (jumlah, thread, invite, GA).
4. Tekan **✅ Ya** untuk mulai. Anda akan melihat progress bar yang
   terupdate beberapa detik sekali, dengan tombol **❌ Cancel & Simpan**.
5. Setelah selesai (atau dibatalkan), tekan **📤 Export Akun** untuk
   mengunduh `accounts.csv`.

Perintah lain: `/myid` (lihat user ID), `/cancel` (batalkan job aktif).

## Deploy ke Koyeb (free web service)

1. Push repo ini ke GitHub.
2. Di [Koyeb Dashboard](https://app.koyeb.com), buat **App → Deploy from GitHub**,
   pilih repo, biarkan auto-detect Dockerfile.
3. Tambahkan **Environment Variables**:
   - `TELEGRAM_BOT_TOKEN` (sebagai *Secret*)
   - `ADMIN_USER_IDS` (opsional, kosongkan untuk publik)
   - `DEFAULT_INVITE_CODE` = `3tb84z` (atau custom)
4. Pastikan service type = **Web**, instance = **Free**, port = **8000**,
   health check path = `/api/health`.
5. Klik Deploy. Setelah status hijau, kirim `/start` ke bot di Telegram.

> ⚠️ **Catatan storage**: Koyeb Free Web Service menggunakan filesystem
> ephemeral. `accounts.csv` dan `proxies.txt` akan hilang setiap kali
> service di-restart/redeploy. Selalu **Export Akun** sebelum redeploy,
> atau gunakan tier berbayar dengan persistent volume di-mount ke `/data`.

## Jalankan lokal

```bash
cd backend
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=...
uvicorn server:app --reload --port 8000
```

Bot akan langsung mulai polling. Buka `http://localhost:8000/api/stats`
untuk health check.
