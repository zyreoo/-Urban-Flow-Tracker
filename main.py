from flask import Flask, request, render_template_string, url_for
import os
import sqlite3
import re
import requests
import asyncio
import aiohttp
from datetime import datetime, timedelta
import pytz

# Get API key from environment variable
GOOGLE_API_KEY = "UR API KEY"
ROMANIA_TIMEZONE = pytz.timezone('Europe/Bucharest')
DATABASE_NAME = "urban_flow.db"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_PATH = os.path.join(BASE_DIR, DATABASE_NAME)

app = Flask(__name__)


def init_db():
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS visited (location TEXT, timestamp TEXT)")
    conn.commit()
    conn.close()

def add_visit(location):
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO visited (location, timestamp) VALUES (?, ?)",
                   (location, datetime.now(ROMANIA_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def get_recent_visits(limit=15):
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM visited ORDER BY timestamp DESC LIMIT ?", (limit,))
    results = cursor.fetchall()
    conn.close()
    return results

def extract_locations(sentence):
    sentence = sentence.lower().strip()
    city_match = re.search(r"in\s+([a-z\s]+)", sentence)
    city = city_match.group(1).strip() if city_match else None
    raw_locations = re.split(r"\bfirst\b|\bthen\b|,|->|\u2192|and then", sentence)
    cleaned_locations = []

    for loc in raw_locations:
        loc = re.sub(r"(i\s+)?go\s+to\s+|visit\s+|the\s+", "", loc).strip()
        if loc:
            if city and city.lower() not in loc.lower():
                cleaned_locations.append(f"{loc.title()}, {city.title()}")
            else:
                cleaned_locations.append(loc.title())
    return [loc for loc in cleaned_locations if loc]

async def fetch_popular_times(session, location):
    base_url = "https://maps.googleapis.com/maps/api/place"
    text_search_url = f"{base_url}/textsearch/json?query={location}&key={GOOGLE_API_KEY}"

    try:
        async with session.get(text_search_url) as response:
            data = await response.json()
            if data["status"] != "OK" or not data.get("results"):
                return None, None, None

            place_id = data["results"][0]["place_id"]
            details_url = f"{base_url}/details/json?placeid={place_id}&fields=opening_hours,popular_times,current_opening_hours&key={GOOGLE_API_KEY}"

            async with session.get(details_url) as details_response:
                details_data = await details_response.json()
                result = details_data.get("result", {})

                opening_hours = result.get("opening_hours", {})
                popular_times = result.get("popular_times", [])
                live = result.get("current_opening_hours", {}).get("live")

                return opening_hours, popular_times, live
    except Exception as e:
        print(f"Error fetching data for {location}: {e}")
        return None, None, None

async def get_popular_times_for_locations(locations):
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_popular_times(session, loc) for loc in locations]
        return await asyncio.gather(*tasks)

def analyze_popular_times(popular_times_data, opening_hours, live_busyness, location_name):
    now = datetime.now(ROMANIA_TIMEZONE)
    today = now.weekday()

    if not popular_times_data:
        if live_busyness is not None:
            return f"Aglomerație curentă: {live_busyness}%"
        return "Informații de aglomerație indisponibile (Google nu oferă date pentru această locație)"

    today_data = next((pt for pt in popular_times_data if pt.get("day") == today), None)
    if today_data and "data" in today_data:
        low = [(t["time"], t["popularity"]) for t in today_data["data"] if t["popularity"] < 30]
        if low:
            return "Ore mai libere: " + ", ".join(f"{t:02d}:00 - {p}%" for t, p in low)
        return "Aglomerație mare în majoritatea zilei."
    return "Fără date detaliate pentru azi."

def calculate_route(locations):
    if len(locations) < 2:
        return [{"error": "Cel puțin 2 locații necesare."}], None

    origin, destination = locations[0], locations[-1]
    waypoints = '|'.join(locations[1:-1]) if len(locations) > 2 else ''
    url = f"https://maps.googleapis.com/maps/api/directions/json?origin={origin}&destination={destination}&waypoints={waypoints}&mode=driving&key={GOOGLE_API_KEY}"

    try:
        response = requests.get(url)
        data = response.json()
        if data["status"] != "OK":
            return [{"error": data["status"]}], None

        legs = data["routes"][0]["legs"]
        total_duration, total_distance = 0, 0
        current_time = datetime.now(ROMANIA_TIMEZONE)

        popular_data = asyncio.run(get_popular_times_for_locations(locations))

        route = []
        for i, leg in enumerate(legs):
            duration_sec = leg["duration"]["value"]
            distance_m = leg["distance"]["value"]
            total_duration += duration_sec
            total_distance += distance_m

            arrival = current_time + timedelta(seconds=total_duration)
            oh, pt, live = popular_data[i]
            best_time = analyze_popular_times(pt, oh, live, locations[i])
            add_visit(locations[i])

            route.append({
                "location": locations[i],
                "duration": leg["duration"]["text"],
                "distance": leg["distance"]["text"],
                "arrival": arrival.strftime("%H:%M"),
                "best_time_to_visit": best_time,
                "is_destination": (i == len(legs)-1)
            })

        maps_embed_url = f"https://www.google.com/maps/embed/v1/directions?key={GOOGLE_API_KEY}&origin={origin}&destination={destination}&mode=driving"
        if waypoints:
            maps_embed_url += f"&waypoints={waypoints}"

        return route, {
            "total_duration": str(timedelta(seconds=total_duration)),
            "total_distance": f"{total_distance / 1000:.2f} km",
            "maps_embed_url": maps_embed_url
        }
    except Exception as e:
        return [{"error": str(e)}], None

@app.route("/", methods=["GET", "POST"])
def index():
    error, route, total = None, None, None
    sentence = request.form.get("itinerary", "").strip() if request.method == "POST" else ""
    visits = get_recent_visits()

    if request.method == "POST" and sentence:
        locations = extract_locations(sentence)
        if len(locations) < 2:
            error = "Te rog introdu cel puțin 2 locații."
        else:
            route, total = calculate_route(locations)

    return render_template_string("""
    <!DOCTYPE html>
    <html lang="ro">
    <head>
        <meta charset="UTF-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <title>Urban Flow Tracker</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="font-sans bg-cover bg-fixed bg-no-repeat" style="background-image: url('{{ url_for('static', filename='fundal.jpg') }}');">
        <main class="max-w-6xl mx-auto p-6 bg-white bg-opacity-90 shadow-lg rounded-xl my-10">
            <h1 class="text-3xl font-bold mb-6 text-center">Urban Flow Tracker</h1>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                <div class="w-full h-[300px] rounded-lg overflow-hidden shadow-inner border">
                    {% if total and total.maps_embed_url %}
                        <iframe width="100%" height="100%" frameborder="0" style="border:0" allowfullscreen
                            src="{{ total.maps_embed_url }}">
                        </iframe>
                    {% else %}
                        <div class="flex items-center justify-center h-full text-xl text-gray-600">Harta va apărea aici după generarea traseului.</div>
                    {% endif %}
                </div>
                <div class="space-y-4">
                    <div class="bg-gray-100 p-4 rounded-lg shadow">
                        <form method="POST">
                            <textarea name="itinerary" rows="3" placeholder="Ex: Piața Bobâlna, Hanul Ciorilor, UM" class="w-full p-2 border border-gray-300 rounded">{{ sentence }}</textarea>
                            <button type="submit" class="mt-2 px-4 py-2 bg-indigo-900 text-white rounded hover:bg-indigo-700">Creează traseu</button>
                        </form>
                        {% if error %}<p class="text-red-600 mt-2">{{ error }}</p>{% endif %}
                    </div>
                    {% if route %}
                    <div class="bg-white p-4 rounded shadow">
                        <h2 class="text-xl font-semibold mb-2">Traseu generat:</h2>
                        {% for item in route %}
                            <div class="mb-4 border border-gray-200 rounded p-3 {% if item.is_destination %}bg-blue-50 border-blue-300{% endif %}">
                                <strong>{{ item.location }}</strong>
                                {% if item.is_destination %}<span class="ml-2 text-blue-600">(Destinație)</span>{% endif %}
                                <br>
                                Durată: {{ item.duration }}<br>
                                Distanță: {{ item.distance }}<br>
                                Sosire: {{ item.arrival }}<br>
                                {{ item.best_time_to_visit }}
                            </div>
                        {% endfor %}
                        <div class="font-semibold mt-4">
                            Durată totală: {{ total.total_duration }}<br>
                            Distanță totală: {{ total.total_distance }}
                        </div>
                    </div>
                    {% endif %}
                    <div class="bg-gray-100 p-4 rounded shadow">
                        <h2 class="text-lg font-bold mb-2">Istoric locații vizitate:</h2>
                        <ul class="list-disc pl-5">
                            {% for v in visits %}
                                <li>{{ v[0] }} - {{ v[1] }}</li>
                            {% endfor %}
                        </ul>
                    </div>
                </div>
            </div>
        </main>
        <footer class="text-center bg-indigo-900 text-white py-4 rounded-b-xl">
         © 2025 Urban Flow Tracker | Creat cu ❤️ în Baia Mare| Contact: <a href="mailto:contact@urbanflow.ro" class="underline text-blue-200">contact@urbanflow.ro</a>
        </footer>
    </body>
    </html>
    """, sentence=sentence, route=route, total=total, error=error, visits=visits)

if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5001)

