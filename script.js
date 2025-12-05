// ** IMPORTANT: Replace 'YOUR_WEATHERAPI_KEY' with the key you copied **
const API_KEY = 'ecfdc1b142a34b4991c185801250412';
const CITY_NAME = 'Kanhangad'; // Use the city you want to display

// Construct the new API URL for WeatherAPI.com
const API_URL = `https://api.weatherapi.com/v1/current.json?key=${API_KEY}&q=${CITY_NAME}&aqi=no`;

// Function to fetch the weather data
function fetchWeather() {
    fetch(API_URL)
        .then(response => {
            if (!response.ok) {
                // WeatherAPI.com returns a 400 for bad requests (like invalid city/key)
                throw new Error('Weather data could not be retrieved. Check API key and city name.');
            }
            return response.json();
        })
        .then(data => {
            // Update: data structure is different! 
            displayWeather(data);
        })
        .catch(error => {
            console.error('Error fetching weather:', error);
            document.getElementById('weather-widget').innerHTML = 
                `<p>Failed to load weather. Check your API key and network connection.</p>`;
        });
}

// Function to update the HTML with the WeatherAPI.com data
function displayWeather(data) {
    const weatherWidget = document.getElementById('weather-widget');
    
    // 💥 Key Difference: Data is now under 'current' and 'location' 💥
    const temp = data.current.temp_c; // Temperature in Celsius
    const description = data.current.condition.text;
    const city = data.location.name;
    const iconUrl = data.current.condition.icon; // URL for the weather icon

    // Create the HTML content to display, including the icon
    const weatherHTML = `
        <h3>Weather in ${city}</h3>
        <p style="font-size: 2em; margin: 5px;">${temp}°C</p>
        <img src="https:${iconUrl}" alt="${description}" style="width: 50px;">
        <p>${description}</p>
    `;

    // Insert the generated HTML into the widget container
    weatherWidget.innerHTML = weatherHTML;
}

// Run the function when the script loads
fetchWeather();

// --- OpenStreetMap (Leaflet.js) Implementation ---

function initializeMap() {
    // 1. Define the Coordinates 
    // Example coordinates for IIT Madras (approximate entrance)
    const khd_coords = [12.314563, 75.093798]; 
    const ZOOM_LEVEL = 15; // 1 (world) to 18 (street view)

    // 2. Initialize the map object, attaching it to the 'map-widget' div
    // 
    const map = L.map('map-widget').setView(khd_coords, ZOOM_LEVEL);

    // 3. Add the OpenStreetMap tiles (the visual background imagery)
  // In your script.js file:
// In your script.js file, inside initializeMap():

L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
    subdomains: 'abcd',
    maxZoom: 20
}).addTo(map);

// The rest of your initializeMap() function (L.marker, etc.) stays the same.;

    // 4. Add a marker (pin) at the specific location
    L.marker(khd_coords).addTo(map)
        .bindPopup('<b>Kanhangad</b>')
        .openPopup();
}

// B. Call the map initialization function to make it run
initializeMap();