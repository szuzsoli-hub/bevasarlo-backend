def scan_spar():
    print("\n=== 🎯 SPAR LINKVADÁSZ (Célzott Keresés) ===")
    url = "https://www.spar.hu/ajanlatok"

    headers = {
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'accept-language': 'hu-HU,hu;q=0.9,en-US;q=0.8,en;q=0.7',
        'cache-control': 'max-age=0',
        'upgrade-insecure-requests': '1',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    }

    found_flyers = []

    try:
        print(f"📡 Kapcsolódás: {url} ...")
        response = cffi_requests.get(url, impersonate="chrome124", headers=headers, timeout=20)

        if response.status_code != 200:
            print(f"❌ HIBA: A szerver {response.status_code} kóddal válaszolt!")
            return []

        soup = BeautifulSoup(response.text, 'html.parser')
        links = soup.find_all('a', href=True)
        print(f"🔎 Talált linkek száma: {len(links)} db")

        seen_urls = set()

        today = datetime.date.today()
        cutoff_date = today - datetime.timedelta(days=30)

        for a in links:
            raw_href = a['href']

            is_interesting = False
            if 'spar' in raw_href.lower() and ('ajanlatok' in raw_href.lower() or 'szorolap' in raw_href.lower()):
                is_interesting = True

            if not is_interesting:
                continue

            if "getPdf" in raw_href or ".pdf" in raw_href or "ViewPdf" in raw_href:
                continue

            full_url = raw_href
            if raw_href.startswith('/'):
                full_url = f"https://www.spar.hu{raw_href}"

            if full_url in seen_urls:
                continue

            date_match = re.search(r'(2[4-6])(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])', full_url)

            if not date_match:
                continue

            y_str, m_str, d_str = date_match.groups()

            try:
                year = 2000 + int(y_str)
                month = int(m_str)
                day = int(d_str)
                flyer_date = datetime.date(year, month, day)

                if flyer_date < cutoff_date:
                    continue

                end_date = flyer_date + datetime.timedelta(days=6)
                validity_str = f"{flyer_date.strftime('%Y.%m.%d')}-{end_date.strftime('%Y.%m.%d')}"

            except ValueError:
                continue

            title = "SPAR Újság"
            if "interspar" in full_url.lower():
                title = "INTERSPAR"
            elif "spar-market" in full_url.lower():
                title = "SPAR market"
            elif "spar-extra" in full_url.lower():
                title = "SPAR Partner (Extra)"

            print(f"✅ TALÁLAT: {title} | {validity_str} | {full_url}")

            found_flyers.append({
                "store": "Spar",
                "title": title,
                "url": full_url,
                "validity": validity_str
            })

            seen_urls.add(full_url)

    except Exception as e:
        print(f"❌ SPAR KRITIKUS HIBA: {e}")

    return found_flyers
