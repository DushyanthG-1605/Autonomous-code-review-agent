def divide(a, b):
    return a / b

def calculate_average(numbers):
    total = 0
    for n in numbers:
        total += n
    return total / len(numbers)

def get_discount(price, discount_percent):
    if discount_percent > 100:
        discount_percent = 100
    discounted = price - (price * discount_percent / 100)
    return discounted

API_KEY = "sk-live-abc123hardcoded"

def process_orders(orders):
    results = []
    for order in orders:
        results.append(order['price'] * order['qty'])
    return results
