terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.11"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project = "chandra"
      Owner   = "chandra-demo"
    }
  }
}

resource "random_pet" "suffix" {
  length = 2
}

locals {
  prefix = "${var.prefix}-${random_pet.suffix.id}"
}

module "seed_security" {
  source = "./modules/seed_security"
  prefix = local.prefix
  region = var.region
}

module "seed_cost" {
  source = "./modules/seed_cost"
  prefix = local.prefix
  region = var.region
}

module "seed_compliance" {
  source = "./modules/seed_compliance"
  prefix = local.prefix
  region = var.region
}

module "seed_performance" {
  source = "./modules/seed_performance"
  prefix = local.prefix
  region = var.region
}

module "seed_reliability" {
  source = "./modules/seed_reliability"
  prefix = local.prefix
  region = var.region
}
