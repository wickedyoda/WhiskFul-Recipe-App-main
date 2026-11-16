import enum

from sqlalchemy import Column, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Table, Text, UniqueConstraint
from sqlalchemy.sql import func

from backend.database import Base


class Store(enum.StrEnum):
    local = "local"
    cloud = "cloud"

class Role(enum.StrEnum):
    admin = "admin"
    user = "user"

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    role = Column(Enum(Role), default=Role.user, nullable=False)
    display_name = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    last_name = Column(String(255), nullable=True)
    username = Column(String(255), unique=True, index=True, nullable=True)
    avatar_url = Column(String(1024), nullable=True)
    is_active = Column(Integer, default=0, nullable=False)
    is_approved = Column(Integer, default=0, nullable=False)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    must_change_password = Column(Integer, default=0, nullable=False)
    password_changed_at = Column(DateTime(timezone=True), nullable=True)
    is_readonly = Column(Integer, default=0, nullable=False)


class PasswordHistory(Base):
    __tablename__ = "password_history"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# Association table: household members (max 7 per household)
household_members = Table(
    "household_members",
    Base.metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("household_id", Integer, ForeignKey("households.id"), nullable=False, index=True),
    Column("user_id", Integer, ForeignKey("users.id"), nullable=False, index=True),
    Column("role", String(50), default="member", nullable=False),
    UniqueConstraint("household_id", "user_id", name="uq_household_user"),
)

# Association table: recipes shared with households
household_recipes = Table(
    "household_recipes",
    Base.metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("household_id", Integer, ForeignKey("households.id"), nullable=False, index=True),
    Column("recipe_id", Integer, ForeignKey("recipes.id"), nullable=False, index=True),
    UniqueConstraint("household_id", "recipe_id", name="uq_household_recipe"),
)


class Household(Base):
    __tablename__ = "households"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    avatar_url = Column(String(1024), nullable=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Cookbook(Base):
    __tablename__ = "cookbooks"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    store = Column(Enum(Store), default=Store.local, nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("idx_cookbooks_owner", "owner_id"),)

class Tag(Base):
    __tablename__ = "tags"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    __table_args__ = (Index("idx_tags_owner", "owner_id"),)

class Recipe(Base):
    __tablename__ = "recipes"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    ingredients = Column(Text, nullable=True)
    instructions = Column(Text, nullable=True)
    source_url = Column(String(1024), nullable=True)
    source_path = Column(String(1024), nullable=True)
    source_filename = Column(String(255), nullable=True)
    store = Column(Enum(Store), default=Store.local, nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    cookbook_id = Column(Integer, ForeignKey("cookbooks.id"), nullable=True, index=True)
    rating = Column(Float, nullable=True)
    flavor_rating = Column(Float, nullable=True)
    effort_rating = Column(Float, nullable=True)
    prep_time_minutes = Column(Integer, nullable=True)
    cook_time_minutes = Column(Integer, nullable=True)
    servings = Column(Integer, nullable=True)
    difficulty = Column(String(50), nullable=True)
    category = Column(String(100), nullable=True)
    subcategory = Column(String(100), nullable=True)
    embedding = Column(Text, nullable=True)
    share_token = Column(String(255), unique=True, index=True, nullable=True)
    household_id = Column(Integer, ForeignKey("households.id"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class RecipeTag(Base):
    __tablename__ = "recipe_tags"
    id = Column(Integer, primary_key=True, index=True)
    recipe_id = Column(Integer, ForeignKey("recipes.id"), nullable=False, index=True)
    tag_id = Column(Integer, ForeignKey("tags.id"), nullable=False, index=True)

class RecipePhoto(Base):
    __tablename__ = "recipe_photos"
    id = Column(Integer, primary_key=True, index=True)
    recipe_id = Column(Integer, ForeignKey("recipes.id"), nullable=False, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    path = Column(String(1024), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("idx_recipe_photos_recipe_id", "recipe_id"),)

class RecipeStepPhoto(Base):
    __tablename__ = "recipe_step_photos"
    id = Column(Integer, primary_key=True, index=True)
    recipe_id = Column(Integer, ForeignKey("recipes.id"), nullable=False, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    step_index = Column(Integer, nullable=False)
    path = Column(String(1024), nullable=False)
    __table_args__ = (Index("idx_recipe_step_photos_recipe_id", "recipe_id"),)
    caption = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class RecipeMedia(Base):
    __tablename__ = "recipe_media"
    id = Column(Integer, primary_key=True, index=True)
    recipe_id = Column(Integer, ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    file_path = Column(String(1024), nullable=False)
    thumbnail_path = Column(String(1024), nullable=True)
    original_filename = Column(String(255), nullable=False)
    media_type = Column(String(20), nullable=False)
    file_size = Column(Integer, nullable=True)
    extracted_text = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("idx_recipe_media_recipe_id", "recipe_id"), Index("idx_recipe_media_owner", "owner_id"))


class Note(Base):
    __tablename__ = "notes"
    id = Column(Integer, primary_key=True, index=True)
    recipe_id = Column(Integer, ForeignKey("recipes.id"), nullable=False, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("idx_notes_recipe_id", "recipe_id"),)

class MealPlan(Base):
    __tablename__ = "meal_plans"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    period = Column(String(50), nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("idx_meal_plans_owner", "owner_id"),)

class MealPlanEntry(Base):
    __tablename__ = "meal_plan_entries"
    id = Column(Integer, primary_key=True, index=True)
    meal_plan_id = Column(Integer, ForeignKey("meal_plans.id"), nullable=False, index=True)
    recipe_id = Column(Integer, ForeignKey("recipes.id"), nullable=False, index=True)
    meal = Column(String(50), nullable=False)
    date = Column(String(20), nullable=True)
    position = Column(Integer, default=0, nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    __table_args__ = (Index("idx_entries_plan_id", "meal_plan_id"), Index("idx_entries_recipe", "recipe_id"),)

class GroceryList(Base):
    __tablename__ = "grocery_lists"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    share_token = Column(String(255), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("idx_grocery_lists_owner", "owner_id"),)

class GroceryItem(Base):
    __tablename__ = "grocery_items"
    id = Column(Integer, primary_key=True, index=True)
    list_id = Column(Integer, ForeignKey("grocery_lists.id"), nullable=False, index=True)
    recipe_id = Column(Integer, ForeignKey("recipes.id"), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    __table_args__ = (Index("idx_grocery_items_list", "list_id"),)
    quantity = Column(String(100), nullable=True)
    checked = Column(Integer, default=0, nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    token = Column(String(255), unique=True, index=True, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class RecipeRating(Base):
    __tablename__ = "recipe_ratings"
    id = Column(Integer, primary_key=True, index=True)
    recipe_id = Column(Integer, ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    score = Column(Integer, nullable=False)  # 0-5
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        Index("idx_ratings_recipe", "recipe_id"),
        Index("idx_ratings_user", "user_id"),
        UniqueConstraint("recipe_id", "user_id", name="uq_recipe_user_rating"),
    )
