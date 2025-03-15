# Price Checker

An asynchronous Python application for tracking product prices across multiple websites and getting alerts when prices change.

## Features

- Monitor prices from multiple e-commerce websites
- Support for both API-based fetching and web scraping
- Automatic notifications when prices change or competitors offer lower prices
- Adaptive rate limiting to respect website limits
- Historical price tracking with SQLite database

## Getting Started

### Prerequisites

- Python 3.9+
- pip

### Installation

1. Clone the repository:
```bash
git clone https://github.com/MrPondi/price-checker.git
cd price-checker
```

2. Install dependencies:
```bash
pip install -e .
```

3. Set up configuration:
Create a `.env` file in the project root with the following content:
```
DATABASE_URL=sqlite:///data/product_prices.db
TARGET_SITE=example.com
NOTIFICATION_URL=https://ntfy.sh/your_channel
```

4. Create an input file:
Create `data/input.json` with your products and site configurations (see example below).

### Usage

Run the application:

```bash
price-checker
```

For scheduled price checking, you can set up a cron job or use a task scheduler.

## Configuration

### Input File Format

The application uses a JSON configuration file (`data/input.json`) with the following structure:

```json
{
  "sites": [
    {
      "root_domain": "api-store.com",
      "category": "api",
      "env_variables": {
        "consumer_key": "your_api_key",
        "consumer_secret": "your_api_secret"
      }
    },
    {
      "root_domain": "scrape-store.com",
      "category": "scrape",
      "selectors": {
        "price": ".product-price",
        "regular_price": ".regular-price",
        "sale_price": ".sale-price"
      },
      "site_rules": {
        "text_contains": {
          "sold out": false
        },
        "element_selector": {
          "out-of-stock": true
        }
      }
    }
  ],
  "products": [
    {
      "product_name": "Example Product",
      "urls": [
        "https://api-store.com/products/123",
        "https://scrape-store.com/products/456"
      ]
    }
  ]
}
```

### Site Configuration

#### API Sites (woocommerce)
For sites that provide API access:
- `root_domain`: The base domain of the site
- `category`: Must be "api"
- `env_variables`: API keys/secrets for authentication

#### Scrape Sites
For sites that require web scraping:
- `root_domain`: The base domain of the site
- `category`: Must be "scrape"
- `selectors`: CSS selectors for price elements
- `site_rules` (optional): Custom rules for parsing site-specific elements

## Project Structure

```
price-checker/
├── src/                    # Main package
│   ├── core/               # Fundamental components
│   │   ├── __init__.py
│   │   ├── database.py
│   │   ├── cache.py
│   │   └── rate_limiter.py
│   │
│   ├── features/           # Feature modules
│   │   ├── __init__.py
│   │   ├── fetchers.py
│   │   └── notifications.py
│   │
│   ├── models.py           # Pydantic/SQLAlchemy models
│   └── utils/              # Helper utilities
│       ├── __init__.py
│       └── logging_config.py
│
├── tests/                  # Unit tests
│   ├── ...
│
├── data/
│   ├── input.json          # Configuration file
│   └── product_prices.db   # SQLite database
├── logs/
├── pyproject.toml          # Project configuration and dependencies
├── setup.py
└── README.md
```

## Running Tests

Run the test suite:

```bash
pytest
```

## Contributing

1. Fork the repository
2. Create your feature branch: `git checkout -b feature/my-new-feature`
3. Commit your changes: `git commit -am 'Add some feature'`
4. Push to the branch: `git push origin feature/my-new-feature`
5. Submit a pull request

## License

This project is licensed under the MIT License - see the LICENSE file for details.
