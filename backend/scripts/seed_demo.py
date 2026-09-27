#!/usr/bin/env python3
"""Seed demo guest account with sample recipes, tags, meal plan, and grocery lists.
Run: python3 backend/scripts/seed_demo.py
"""

import os
import secrets
import sys

import bcrypt
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from models import (
    GroceryItem,
    GroceryList,
    MealPlan,
    MealPlanEntry,
    PasswordHistory,
    Recipe,
    RecipeTag,
    Tag,
    User,
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///./recipes.db')
DATABASE_URL = DATABASE_URL.replace('mysql://', 'mysql+pymysql://')
try:
    from database import SessionLocal
except ImportError:
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(bind=engine)

GUEST_EMAIL = 'guest@whiskful.app'
# Read from the environment rather than hardcoding: a committed default password
# becomes a known credential for any deployment that seeds demo data without
# overriding it. Seeding fails fast with a clear message if it is unset.
GUEST_PASSWORD = os.environ.get("DEMO_GUEST_PASSWORD", "")
if not GUEST_PASSWORD:
    raise SystemExit(
        "DEMO_GUEST_PASSWORD is not set. Export a password before seeding demo "
        "data, e.g. DEMO_GUEST_PASSWORD=\"$(openssl rand -base64 18)\"."
    )

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def create_demo_user(db):
    """Create or update the demo guest account."""
    user = db.query(User).filter(User.email == GUEST_EMAIL).first()
    password_hash = hash_password(GUEST_PASSWORD)

    if user:
        user.hashed_password = password_hash
        user.is_active = 1
        user.is_approved = 1
        user.is_readonly = 1
        user.display_name = 'Demo Chef'
        print(f"Updated existing demo user: {user.email}")
    else:
        user = User(
            email=GUEST_EMAIL,
            display_name='Demo Chef',
            username='guest',
            hashed_password=password_hash,
            is_active=1,
            is_approved=1,
            is_readonly=1
        )
        db.add(user)
        db.flush()
        print(f"Created demo user: {user.email}")

    ph = PasswordHistory(user_id=user.id, hashed_password=password_hash)
    db.add(ph)

    return user

def create_tag(db, user_id, name):
    tag = db.query(Tag).filter(Tag.name == name).first()
    if not tag:
        tag = Tag(name=name, owner_id=user_id)
        db.add(tag)
        db.flush()
    return tag

def create_recipe(db, user_id, title, category, description, ingredients, instructions, prep_time=None, cook_time=None, servings=None, difficulty=None, tags=None, photo=None):
    recipe = Recipe(
        owner_id=user_id,
        title=title,
        category=category,
        description=description,
        ingredients=ingredients,
        instructions=instructions,
        prep_time_minutes=prep_time,
        cook_time_minutes=cook_time,
        servings=servings,
        difficulty=difficulty,
        source_url=photo
    )
    db.add(recipe)
    db.flush()

    if tags:
        for tag_name in tags:
            tag = create_tag(db, user_id, tag_name)
            recipe_tag = RecipeTag(recipe_id=recipe.id, tag_id=tag.id)
            db.add(recipe_tag)

    return recipe

def create_grocery_list(db, user_id, name, items):
    grocery_list = GroceryList(owner_id=user_id, name=name, share_token=secrets.token_hex(8))
    db.add(grocery_list)
    db.flush()

    for item_text in items:
        item = GroceryItem(list_id=grocery_list.id, name=item_text, checked=0, owner_id=user_id)
        db.add(item)

    return grocery_list

def create_meal_plan(db, user_id, name, period, entries):
    meal_plan = MealPlan(name=name, period=period, owner_id=user_id)
    db.add(meal_plan)
    db.flush()

    for idx, (recipe_id, meal) in enumerate(entries):
        item = MealPlanEntry(meal_plan_id=meal_plan.id, recipe_id=recipe_id, meal=meal, position=idx, owner_id=user_id)
        db.add(item)

    return meal_plan


# (title, category, description, ingredients, instructions, prep, cook, servings, difficulty, tags)
DEMO_RECIPES = [
    ('Pancakes', 'Breakfast',
     'Fluffy buttermilk pancakes with real maple syrup.',
     '- 1 cup flour\n- 2 tbsp sugar\n- 2 tsp baking powder\n- 1/2 tsp salt\n- 1 egg\n- 1/2 cup milk\n- 2 tbsp butter (melted)',
     '1. Mix dry ingredients.\n2. Add wet ingredients.\n3. Cook on skillet until bubbles form.\n4. Serve with syrup.',
     10, 15, 4, 'Easy', ['Breakfast', 'Easy', 'Comfort Food']),
    ('Greek Yogurt Bowl', 'Breakfast',
     'Creamy Greek yogurt with honey, nuts, and fresh fruit.',
     '- 1 cup Greek yogurt\n- 1 tbsp honey\n- 2 tbsp granola\n- 1/2 cup mixed berries\n- 1 tbsp chopped nuts',
     '1. Layer yogurt in a bowl.\n2. Top with honey, granola, berries, and nuts.',
     5, 0, 2, 'Easy', ['Breakfast', 'Vegetarian', 'Gluten-Free']),
    ('Avocado Toast', 'Lunch',
     'Simple avocado toast with chili flakes and lemon.',
     '- 2 slices sourdough bread\n- 1 ripe avocado\n- 1 tbsp olive oil\n- 1/2 lemon\n- Chili flakes\n- Salt and pepper',
     '1. Toast bread.\n2. Mash avocado with lemon juice.\n3. Spread on toast.\n4. Drizzle with olive oil, season.',
     5, 5, 2, 'Easy', ['Lunch', 'Vegetarian', 'Quick']),
    ('Scrambled Eggs', 'Breakfast',
     'Classic fluffy scrambled eggs with chives.',
     '- 4 eggs\n- 2 tbsp milk\n- 1 tbsp butter\n- Salt and pepper\n- 2 tbsp fresh chives, chopped',
     '1. Whisk eggs with milk, salt, and pepper.\n2. Melt butter in pan over medium heat.\n3. Pour eggs, stir gently until set.\n4. Top with chives.',
     5, 5, 2, 'Easy', ['Breakfast', 'Vegetarian', 'Gluten-Free', 'Easy']),
    ('Oatmeal', 'Breakfast',
     'Warm, comforting oatmeal with cinnamon and maple.',
     '- 1/2 cup rolled oats\n- 1 cup milk\n- 1 tbsp maple syrup\n- 1/2 tsp cinnamon\n- Pinch of salt',
     '1. Combine oats, milk, and salt in a pot.\n2. Cook over medium heat, stirring, until thick.\n3. Stir in maple syrup and cinnamon.',
     5, 10, 2, 'Easy', ['Breakfast', 'Vegetarian', 'Easy', 'Comfort Food']),
    ('Smoothie Bowl', 'Breakfast',
     'Thick smoothie bowl topped with fresh fruit and granola.',
     '- 1 frozen banana\n- 1/2 cup frozen berries\n- 1/2 cup Greek yogurt\n- 1/2 cup almond milk\n- 2 tbsp granola',
     '1. Blend banana, berries, yogurt, and milk until thick.\n2. Pour into bowl. Top with granola and fresh fruit.',
     5, 0, 2, 'Easy', ['Breakfast', 'Vegetarian', 'Gluten-Free', 'Quick']),
    ('Caprese Salad', 'Appetizer',
     'Classic Italian Caprese with fresh mozzarella, tomatoes, and basil.',
     '- 2 large tomatoes, sliced\n- 8 oz fresh mozzarella, sliced\n- 1/4 cup fresh basil\n- 2 tbsp olive oil\n- Salt and pepper to taste',
     '1. Layer tomatoes and mozzarella on a plate.\n2. Drizzle with olive oil.\n3. Top with basil and season.',
     10, 0, 4, 'Easy', ['Appetizer', 'Italian', 'Vegetarian']),
    ('Chicken Salad', 'Lunch',
     'Creamy chicken salad with celery and grapes.',
     '- 2 cups cooked chicken, shredded\n- 1/2 cup mayo\n- 1/4 cup celery, diced\n- 1/4 cup grapes, halved\n- Salt and pepper',
     '1. Mix all ingredients in a bowl.\n2. Season to taste.',
     10, 0, 4, 'Easy', ['Lunch', 'Gluten-Free']),
    ('Mediterranean Wrap', 'Lunch',
     'Whole wheat wrap with hummus, veggies, and feta.',
     '- 1 whole wheat tortilla\n- 2 tbsp hummus\n- 1/4 cup cucumber, sliced\n- 1/4 cup bell peppers, sliced\n- 2 tbsp feta cheese\n- Handful of spinach',
     '1. Spread hummus on tortilla.\n2. Layer veggies and feta.\n3. Roll tightly and slice.',
     5, 0, 2, 'Easy', ['Lunch', 'Vegetarian', 'Gluten-Free', 'Quick']),
    ('Tomato Soup', 'Lunch',
     'Creamy tomato soup with a hint of basil.',
     '- 1 can (28oz) whole tomatoes\n- 1 onion, diced\n- 2 cloves garlic, minced\n- 2 cups vegetable broth\n- 1/2 cup cream\n- Fresh basil',
     '1. Saute onion and garlic.\n2. Add tomatoes and broth. Simmer 20 min.\n3. Blend until smooth.\n4. Stir in cream and season.',
     10, 25, 6, 'Medium', ['Lunch', 'Vegetarian', 'Comfort Food']),
    ('Grilled Cheese', 'Lunch',
     'Classic grilled cheese with crispy edges.',
     '- 2 slices bread\n- 2 slices cheddar cheese\n- 1 tbsp butter',
     '1. Butter bread. Place cheese between slices.\n2. Grill in pan until golden and cheese melts.',
     2, 5, 1, 'Easy', ['Lunch', 'Vegetarian', 'Comfort Food', 'Quick']),
    ('Spaghetti Bolognese', 'Main',
     'Classic Italian pasta with rich meat sauce.',
     '- 8 oz spaghetti\n- 1 lb ground beef\n- 1 onion, diced\n- 3 cloves garlic, minced\n- 1 (28oz) can tomatoes\n- 2 tbsp tomato paste\n- Salt, pepper, oregano',
     '1. Cook pasta.\n2. Brown beef with onion and garlic.\n3. Add tomatoes, paste, and spices. Simmer 30 min.\n4. Serve over pasta.',
     15, 45, 6, 'Medium', ['Main', 'Italian', 'Comfort Food']),
    ('Beef Stir Fry', 'Main',
     'Quick beef stir fry with vegetables and soy sauce.',
     '- 1 lb beef, thinly sliced\n- 2 cups mixed vegetables\n- 3 cloves garlic, minced\n- 2 tbsp soy sauce\n- 1 tbsp oil\n- 1 tsp ginger',
     '1. Heat oil in pan.\n2. Cook beef until browned.\n3. Add vegetables and stir-fry 5 min.\n4. Add garlic, ginger, soy sauce. Cook 2 min.',
     15, 10, 4, 'Medium', ['Main', 'Quick', 'Comfort Food']),
    ('Chicken Alfredo', 'Main',
     'Creamy pasta with grilled chicken and parmesan.',
     '- 8 oz fettuccine\n- 2 chicken breasts\n- 1 cup heavy cream\n- 1/2 cup parmesan, grated\n- 3 cloves garlic, minced\n- 2 tbsp butter\n- Salt and pepper',
     '1. Cook pasta. Grill chicken, slice.\n2. Heat butter, add garlic.\n3. Add cream, simmer 5 min. Add parmesan.\n4. Toss with pasta and chicken.',
     15, 20, 4, 'Medium', ['Main', 'Italian', 'Comfort Food']),
    ('Vegetable Curry', 'Main',
     'Spicy coconut curry with mixed vegetables.',
     '- 1 can coconut milk\n- 2 cups mixed vegetables\n- 2 tbsp curry powder\n- 1 tbsp ginger, grated\n- 2 cloves garlic, minced\n- 1 tbsp oil',
     '1. Heat oil, saute garlic and ginger.\n2. Add vegetables, curry powder. Cook 5 min.\n3. Add coconut milk. Simmer 15 min.',
     10, 25, 4, 'Medium', ['Main', 'Vegetarian', 'Vegan', 'Gluten-Free']),
    ('Tacos', 'Main',
     'Seasoned ground beef tacos with all the fixings.',
     '- 1 lb ground beef\n- 1 packet taco seasoning\n- 6 taco shells\n- 1/2 cup cheese, shredded\n- 1/4 cup lettuce, shredded\n- 2 tomatoes, diced\n- 1/4 cup salsa',
     '1. Brown beef, add seasoning.\n2. Warm shells.\n3. Fill with beef, cheese, lettuce, tomatoes, salsa.',
     10, 15, 4, 'Easy', ['Main', 'Mexican', 'Comfort Food']),
    ('Salmon with Vegetables', 'Main',
     'Pan-seared salmon with roasted vegetables.',
     '- 2 salmon fillets\n- 2 cups mixed vegetables\n- 2 tbsp olive oil\n- 1 lemon\n- Salt and pepper',
     '1. Season salmon. Heat oil in pan.\n2. Sear salmon 4-5 min per side.\n3. Roast vegetables. Serve with lemon.',
     10, 20, 2, 'Medium', ['Main', 'Gluten-Free']),
    ('Ramen', 'Main',
     'Quick miso ramen with soft-boiled egg and greens.',
     '- 2 packs ramen noodles\n- 4 cups chicken broth\n- 2 tbsp miso paste\n- 1 soft-boiled egg\n- 1/2 cup bok choy\n- 2 green onions, sliced',
     '1. Boil noodles. Heat broth with miso.\n2. Add bok choy. Serve with noodles, egg, onions.',
     5, 10, 2, 'Easy', ['Main', 'Comfort Food', 'Quick']),
    ('Lasagna', 'Main',
     'Layered Italian lasagna with ricotta and marinara.',
     '- 8 lasagna noodles\n- 1 lb ground beef\n- 15 oz ricotta\n- 2 cups mozzarella, shredded\n- 1 (24oz) marinara sauce\n- 1/2 cup parmesan',
     '1. Cook noodles. Brown beef.\n2. Mix ricotta with egg.\n3. Layer: sauce, noodles, ricotta, beef, mozzarella.\n4. Bake 350F for 30 min.',
     20, 45, 8, 'Hard', ['Main', 'Italian', 'Comfort Food']),
    ('Pasta Aglio e Olio', 'Main',
     'Simple Italian pasta with garlic, olive oil, and chili.',
     '- 8 oz spaghetti\n- 4 cloves garlic, sliced\n- 1/4 cup olive oil\n- 1/4 tsp red chili flakes\n- 1/4 cup parsley, chopped\n- 1/2 cup parmesan',
     '1. Cook pasta. Reserve 1/2 cup pasta water.\n2. Heat oil, cook garlic and chili.\n3. Toss with pasta, water, parsley, parmesan.',
     5, 10, 4, 'Easy', ['Main', 'Italian', 'Vegetarian', 'Quick']),
    ('One-Pot Rice', 'Main',
     'Easy one-pot rice with chicken and vegetables.',
     '- 1 cup rice\n- 1.5 cups broth\n- 1 chicken breast, diced\n- 1 cup mixed vegetables\n- 1 tbsp oil',
     '1. Heat oil, brown chicken.\n2. Add rice, broth, vegetables.\n3. Bring to boil, cover, simmer 20 min.',
     5, 20, 4, 'Easy', ['Main', 'Comfort Food', 'Quick']),
    ('Quesadilla', 'Main',
     'Cheesy quesadilla with bell peppers and onions.',
     '- 2 flour tortillas\n- 1 cup cheese, shredded\n- 1/4 cup bell pepper, sliced\n- 1/4 cup onion, sliced\n- 1 tbsp oil',
     '1. Heat oil in pan.\n2. Layer cheese, veggies on tortilla.\n3. Fold, cook until golden and cheese melts.',
     5, 10, 2, 'Easy', ['Main', 'Mexican', 'Quick']),
    ('Chocolate Chip Cookies', 'Dessert',
     'Classic chewy chocolate chip cookies straight from the oven.',
     '- 2 1/4 cups flour\n- 1 tsp baking soda\n- 1 tsp salt\n- 1 cup butter, softened\n- 3/4 cup sugar\n- 3/4 cup brown sugar\n- 2 eggs\n- 2 tsp vanilla\n- 2 cups chocolate chips',
     '1. Preheat oven to 375F.\n2. Mix dry ingredients.\n3. Cream butter and sugars.\n4. Add eggs and vanilla. Mix in flour.\n5. Fold in chocolate chips.\n6. Bake 10-12 min.',
     15, 12, 24, 'Easy', ['Dessert', 'Easy']),
    ('Brownies', 'Dessert',
     'Fudgy homemade brownies with walnuts.',
     '- 1/2 cup butter\n- 1 cup sugar\n- 2 eggs\n- 1/3 cup cocoa powder\n- 1/2 cup flour\n- 1/4 tsp salt\n- 1/4 cup walnuts, chopped',
     '1. Melt butter. Add sugar, eggs.\n2. Stir in cocoa, flour, salt.\n3. Fold in walnuts.\n4. Bake 350F for 25 min.',
     10, 25, 16, 'Easy', ['Dessert', 'Easy', 'Vegetarian']),
    ('Cheesecake', 'Dessert',
     'Creamy New York-style cheesecake with graham cracker crust.',
     '- 1 1/2 cups graham cracker crumbs\n- 1/3 cup butter, melted\n- 4 (8oz) cream cheese, softened\n- 1 cup sugar\n- 4 eggs\n- 1/2 cup sour cream',
     '1. Mix crust ingredients. Press into pan.\n2. Beat cream cheese, sugar. Add eggs one at a time.\n3. Mix in sour cream.\n4. Bake 325F for 55 min.',
     20, 55, 12, 'Hard', ['Dessert', 'Vegetarian']),
    ('Apple Pie', 'Dessert',
     'Classic apple pie with flaky crust and spiced filling.',
     '- 2 pie crusts\n- 6 cups apples, peeled and sliced\n- 3/4 cup sugar\n- 2 tbsp flour\n- 1 tsp cinnamon\n- 1/4 tsp nutmeg',
     '1. Mix filling ingredients.\n2. Place one crust in pie dish. Add filling.\n3. Cover with top crust. Seal edges. Vent.\n4. Bake 375F for 50 min.',
     20, 50, 8, 'Hard', ['Dessert', 'Vegetarian', 'Comfort Food']),
    ('Garlic Bread', 'Appetizer',
     'Toasted garlic bread with fresh herbs.',
     '- 1 baguette\n- 4 tbsp butter, softened\n- 3 cloves garlic, minced\n- 2 tbsp parsley, chopped\n- 1/4 cup parmesan',
     '1. Mix butter, garlic, parsley.\n2. Spread on baguette slices.\n3. Bake 400F for 10 min.',
     10, 10, 4, 'Easy', ['Appetizer', 'Vegetarian', 'Italian']),
    ('Deviled Eggs', 'Appetizer',
     'Classic deviled eggs with paprika and mayo.',
     '- 6 hard-boiled eggs\n- 3 tbsp mayo\n- 1 tsp mustard\n- Paprika\n- Salt and pepper',
     '1. Halve eggs, remove yolks.\n2. Mash yolks with mayo, mustard.\n3. Fill whites. Dust with paprika.',
     10, 0, 6, 'Easy', ['Appetizer', 'Vegetarian', 'Gluten-Free']),
    ('Bruschetta', 'Appetizer',
     'Italian appetizer with tomatoes, basil, and garlic on toasted bread.',
     '- 1 baguette, sliced\n- 3 tomatoes, diced\n- 2 cloves garlic, minced\n- 1/4 cup basil, chopped\n- 2 tbsp olive oil\n- Salt and pepper',
     '1. Toast bread. Rub with garlic.\n2. Mix tomatoes, basil, oil. Season.\n3. Top bread with mixture.',
     10, 5, 6, 'Easy', ['Appetizer', 'Italian', 'Vegetarian']),
    ('Guacamole', 'Appetizer',
     'Fresh avocado dip with lime and cilantro.',
     '- 2 ripe avocados\n- 1 lime, juiced\n- 1/4 cup cilantro, chopped\n- 1/4 cup onion, diced\n- 1 jalapeno, seeded and minced\n- Salt to taste',
     '1. Mash avocados.\n2. Add lime juice.\n3. Stir in remaining ingredients.',
     10, 0, 4, 'Easy', ['Appetizer', 'Mexican', 'Vegetarian', 'Gluten-Free']),
    ('Hummus', 'Appetizer',
     'Creamy homemade hummus with olive oil and paprika.',
     '- 1 can chickpeas\n- 2 tbsp tahini\n- 2 cloves garlic\n- 2 tbsp lemon juice\n- 2 tbsp olive oil\n- Paprika for garnish',
     '1. Drain chickpeas, reserve liquid.\n2. Blend all ingredients with some liquid.\n3. Drizzle with oil, dust with paprika.',
     5, 0, 4, 'Easy', ['Appetizer', 'Vegetarian', 'Vegan', 'Gluten-Free']),
    ('Chicken Noodle Soup', 'Soup',
     'Classic comforting chicken noodle soup.',
     '- 6 cups chicken broth\n- 2 chicken breasts\n- 2 carrots, diced\n- 2 celery stalks, diced\n- 1 onion, diced\n- 2 cups egg noodles\n- 2 cloves garlic',
     '1. Simmer broth with chicken 20 min. Shred.\n2. Add vegetables. Cook 10 min.\n3. Add noodles. Cook until tender.',
     15, 30, 6, 'Medium', ['Soup', 'Comfort Food']),
    ('Minestrone', 'Soup',
     'Hearty Italian vegetable soup with beans and pasta.',
     '- 1 can beans\n- 1 zucchini, diced\n- 1 carrot, diced\n- 1 onion, diced\n- 2 cups spinach\n- 1/2 cup pasta\n- 4 cups vegetable broth',
     '1. Saute onion, carrot, zucchini.\n2. Add broth, beans, pasta. Simmer 15 min.\n3. Add spinach. Cook 5 min.',
     10, 25, 6, 'Medium', ['Soup', 'Vegetarian', 'Italian']),
    ('Butternut Squash Soup', 'Soup',
     'Creamy roasted butternut squash soup.',
     '- 1 butternut squash\n- 1 onion, chopped\n- 3 cups vegetable broth\n- 1/2 cup cream\n- 1 tsp ginger',
     '1. Roast squash until tender. Scoop flesh.\n2. Saute onion. Add squash, broth, ginger.\n3. Simmer 20 min. Blend. Stir in cream.',
     15, 35, 6, 'Medium', ['Soup', 'Vegetarian', 'Gluten-Free']),
    ('Rice Bowl', 'Main',
     'Bowl with rice, veggies, and protein.',
     '- 1 cup rice\n- 1/2 cup edamame\n- 1/4 cup cucumber, sliced\n- 1/4 avocado\n- 1 tbsp soy sauce\n- 1 tsp sesame oil',
     '1. Cook rice.\n2. Arrange all ingredients in bowl.\n3. Drizzle with soy and sesame oil.',
     10, 10, 2, 'Easy', ['Main', 'Vegetarian', 'Gluten-Free', 'Quick']),
    ('Pasta Pomodoro', 'Main',
     'Simple pasta with fresh tomato sauce and basil.',
     '- 8 oz spaghetti\n- 4 ripe tomatoes, diced\n- 2 cloves garlic, minced\n- 1/4 cup basil, chopped\n- 2 tbsp olive oil\n- Salt and pepper',
     '1. Cook pasta.\n2. Saute garlic, add tomatoes. Cook 10 min.\n3. Toss with pasta, basil, oil. Season.',
     5, 15, 4, 'Easy', ['Main', 'Italian', 'Vegetarian', 'Gluten-Free']),
]

def main():
    db = SessionLocal()
    try:
        user = create_demo_user(db)
        user_id = user.id

        # Clear existing demo data (disable FK checks to avoid constraint errors)
        db.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        db.query(Recipe).filter(Recipe.owner_id == user_id).delete(synchronize_session=False)
        db.query(Tag).filter(Tag.owner_id == user_id).delete(synchronize_session=False)
        db.query(GroceryList).filter(GroceryList.owner_id == user_id).delete(synchronize_session=False)
        db.query(MealPlan).filter(MealPlan.owner_id == user_id).delete(synchronize_session=False)
        db.query(PasswordHistory).filter(PasswordHistory.user_id == user_id).delete(synchronize_session=False)
        db.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        db.flush()

        # --- Tags ---
        all_tags = [
            'Breakfast', 'Lunch', 'Dinner', 'Dessert', 'Appetizer',
            'Main', 'Soup', 'Vegetarian', 'Vegan', 'Gluten-Free',
            'Easy', 'Quick', 'Comfort Food', 'Italian', 'Mexican',
            'Hard', 'Medium'
        ]
        for tag_name in all_tags:
            create_tag(db, user_id, tag_name)

        # --- Recipes ---
        recipe_ids = []
        for title, category, desc, ingredients, instructions, prep, cook, serv, diff, tags in DEMO_RECIPES:
            r = create_recipe(
                db, user_id, title, category, desc,
                ingredients, instructions,
                prep_time=prep, cook_time=cook, servings=serv, difficulty=diff,
                tags=tags
            )
            recipe_ids.append(r.id)

        # --- Grocery Lists ---
        create_grocery_list(db, user_id, 'Weekly Shopping', [
            'Milk', 'Eggs', 'Bread', 'Tomatoes', 'Chicken breasts',
            'Avocado', 'Cheese', 'Onions', 'Garlic', 'Spinach'
        ])
        create_grocery_list(db, user_id, 'Dessert Night', [
            'Chocolate chips', 'Flour', 'Butter', 'Brown sugar', 'Vanilla extract'
        ])

        # --- Meal Plan ---
        create_meal_plan(db, user_id, 'This Week', 'week', [
            (recipe_ids[0], 'breakfast'),  # Pancakes
            (recipe_ids[6], 'lunch'),      # Avocado Toast
            (recipe_ids[11], 'dinner'),    # Spaghetti Bolognese
            (recipe_ids[22], 'dessert'),   # Chocolate Chip Cookies
        ])

        db.commit()
        # Do not echo the password: CI logs are retained and often readable by
        # anyone with repo or Actions access.
        print(f"\n✅ Seeded demo guest account: {GUEST_EMAIL}")
        print("   (password was read from DEMO_GUEST_PASSWORD and is not shown)")
        print(f"   - {len(DEMO_RECIPES)} recipes")
        print(f"   - {len(all_tags)} tags")
        print("   - 2 grocery lists (10 + 5 items)")
        print("   - 1 meal plan (4 entries)")

    except Exception as e:
        db.rollback()
        print(f"❌ Error: {e}")
        raise
    finally:
        db.close()

if __name__ == '__main__':
    main()
