# --- Network foundation ------------------------------------------------------
# Everything else (RDS, ElastiCache, ECS) attaches to this VPC.
#
# COST: VPC, subnets, internet gateway, route tables and security groups are all
# FREE. The only paid network resource is a NAT Gateway (~$33/mo), which we
# deliberately skip (see private subnets below). So this whole layer costs $0 to
# leave up — the hourly charges start in Phase D (RDS/ElastiCache) and E (Fargate/ALB).

variable "vpc_cidr" {
  description = "CIDR block for the VPC (65k addresses)."
  type        = string
  default     = "10.0.0.0/16"
}

# Use the first two available AZs in the region. Two AZs is the minimum for HA:
# RDS multi-AZ needs two, and an ALB requires subnets in >=2 AZs.
data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # so RDS/ElastiCache endpoints get resolvable DNS names
  tags                 = { Name = "${var.project}-vpc" }
}

# --- Public subnets: reachable from the internet -----------------------------
# The ALB lives here, and (per the low-cost decision) the Fargate tasks too, with
# public IPs — that's how they pull images / reach Stripe without a NAT Gateway.
resource "aws_subnet" "public" {
  count                   = length(local.azs)
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index) # 10.0.0.0/24, 10.0.1.0/24
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.project}-public-${local.azs[count.index]}" }
}

# --- Private subnets: the data tier, never exposed to the internet -----------
# RDS Postgres + ElastiCache Redis go here. They need no inbound internet, and
# (with no NAT) no outbound either — they only talk within the VPC.
resource "aws_subnet" "private" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 10) # 10.0.10.0/24, 10.0.11.0/24
  availability_zone = local.azs[count.index]
  tags              = { Name = "${var.project}-private-${local.azs[count.index]}" }
}

# --- Internet gateway + public routing ---------------------------------------
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.project}-igw" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0" # anything not local -> out via the internet gateway
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${var.project}-public-rt" }
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Explicit private route table with NO internet route — just the implicit local
# route, which is all RDS/Redis need. (If a private service ever needs outbound
# internet, this is where a NAT Gateway route would go.)
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.project}-private-rt" }
}

resource "aws_route_table_association" "private" {
  count          = length(aws_subnet.private)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}
