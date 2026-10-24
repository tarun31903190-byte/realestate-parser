# Real Estate WhatsApp Parser

## Setup (15 minute)

1. **API key**: https://aistudio.google.com par jao -> "Get API key" -> free key banao.
2. **GitHub**: naya repo banao (Private rakho) aur ye 3 files upload karo: `app.py`, `requirements.txt`, `README.md`.
3. **Streamlit**: https://share.streamlit.io par GitHub se login karo -> "New app" -> repo select karo -> main file `app.py`.
4. Deploy dabane se pehle **Advanced settings -> Secrets** mein ye paste karo:

```
GEMINI_API_KEY = "yahan_apni_key"
# Optional: agar default model error de to yahan naya model name likho
# MODEL = "gemini-2.5-flash"
```

5. Deploy karo. Live URL mil jayega.

## WhatsApp chat export
Group -> More -> Export chat -> **Without media** -> `.txt` file save karo.

## Dhyan rakhne wali baatein
- API key kabhi code mein ya public GitHub par mat daalo.
- Free Gemini tier ki daily request limit hoti hai. Bahut bada chat ho to app dheere chalega ya kuch batches fail ho sakte hain; dobara run karo.
- Free tier mein Google data ko improve karne ke liye use kar sakta hai. Client ke private numbers hon to paid tier sochna.
- Database (Supabase / Google Sheets) agla step hai; abhi app CSV download deta hai.
