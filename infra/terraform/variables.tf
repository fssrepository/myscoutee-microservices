variable "verified_cloud_image" {
  description = "Absolute local path to the checksum-verified pinned Ubuntu image."
  type        = string
}
variable "ssh_public_key_file" {
  description = "Lab-owned SSH public key; the private key stays outside Terraform and Git."
  type        = string
}
variable "network_cidr" {
  type    = string
  default = "192.168.173.0/24"
  validation {
    condition     = can(cidrhost(var.network_cidr, 20)) && endswith(var.network_cidr, "/24") && can(regex("^(10\\.|192\\.168\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.)", var.network_cidr))
    error_message = "Use a non-overlapping private IPv4 /24 lab subnet."
  }
}
variable "memory_mib" {
  type    = number
  default = 8192
  validation {
    condition     = var.memory_mib >= 4096 && var.memory_mib <= 10240
    error_message = "Keep the local VM between 4 and 10 GiB; check host capacity before apply."
  }
}
variable "running" {
  type    = bool
  default = true
}

variable "lab_name" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,62}$", var.lab_name))
    error_message = "Use a safe, project-owned libvirt resource name."
  }
}
variable "bridge_name" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,14}$", var.bridge_name))
    error_message = "Use a safe Linux interface name of at most 15 characters."
  }
}
variable "mac_address" {
  type = string
  validation {
    condition     = can(regex("^52:54:00(:[0-9a-f]{2}){3}$", var.mac_address))
    error_message = "Use an explicit, unique QEMU MAC address."
  }
}
