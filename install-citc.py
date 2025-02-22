#! /usr/bin/env python

from __future__ import print_function, unicode_literals

import argparse
import os
import os.path
import stat
import subprocess
import sys
import shutil
import tarfile
import time
from subprocess import call, check_call, check_output, CalledProcessError
try:
    from urllib.request import urlretrieve
except ImportError:
    from urllib import urlretrieve
from zipfile import ZipFile


def main():
    parser = argparse.ArgumentParser(description="Install Cluster in the Cloud")
    parser.add_argument("csp", choices=["aws"], help="Which cloud provider to install into")
    parser.add_argument("--dry-run", help="Perform a dry run", action="store_true")
    parser.add_argument("--region", help="AWS region")
    parser.add_argument("--availability_zone", help="AWS availability zone")
    parser.add_argument("--profile", help="AWS credentials profile")
    parser.add_argument("--terraform-repo", default="clusterinthecloud/terraform", help="CitC Terraform GitHub project repo to use")
    parser.add_argument("--terraform-branch", default="master", help="CitC Terraform branch to use")
    parser.add_argument("--ansible-repo", help="CitC Ansible repo to use")
    parser.add_argument("--ansible-branch", help="CitC Ansible branch to use")
    args = parser.parse_args()

    print("Installing Cluster in the Cloud on AWS")

    if not args.dry_run:
        try:
            check_command = ["aws", "--dry-run", "ec2", "describe-images"]
            if args.profile:
                check_command.extend(["--profile", args.profile])
            if args.region:
                check_command.extend(["--region", args.region])
            check_output(check_command, stderr=subprocess.STDOUT)
        except CalledProcessError as e:
            if "RequestExpired" in e.output.decode():
                print("AWS credentials have expired:")
            if "DryRunOperation" not in e.output.decode():
                print(e.output.decode())
                exit(1)

    # Download the CitC Terraform repo
    print("Downloading CitC Terraform configuration")
    tf_repo_tar, _ = urlretrieve("https://github.com/{repo}/archive/{branch}.tar.gz".format(repo=args.terraform_repo, branch=args.terraform_branch))
    tarfile.open(tf_repo_tar).extractall()
    shutil.rmtree("citc-terraform", ignore_errors=True)
    os.rename("terraform-{branch}".format(branch=args.terraform_branch), "citc-terraform")
    os.chdir("citc-terraform")

    terraform = download_terraform("1.0.3")

    # Create key for admin and provisioning
    if not os.path.isfile("citc-key"):
        check_call(["ssh-keygen", "-t", "ed25519", "-f", "citc-key", "-N", ""])

    # Intialise Terraform
    check_call([terraform, "-chdir={}".format(args.csp), "init"])
    check_call([terraform, "-chdir={}".format(args.csp), "validate"])

    # Set up the variable file
    config_file(args.csp, args)

    # Create the cluster
    if not args.dry_run:
        check_call([terraform, "-chdir={}".format(args.csp), "apply", "-auto-approve"])

        # Get the outputs
        ip = check_output([terraform, "-chdir={}".format(args.csp), "output", "-no-color", "-raw", "-state=terraform.tfstate", "ManagementPublicIP"]).decode().strip().strip('"')
        cluster_id = check_output([terraform, "-chdir={}".format(args.csp), "output", "-no-color", "-raw", "-state=terraform.tfstate", "cluster_id"]).decode().strip().strip('"')
    else:
        print("... pretending to create the cluster ...")
        ip = "1.1.1.1"
        cluster_id = "test-cluster"

    # Upload the config to the cluster
    os.chdir("..")
    new_dir_name = "citc-terraform-{}".format(cluster_id)
    os.rename("citc-terraform", new_dir_name)

    key_path = "{}/citc-key".format(new_dir_name)

    shutil.rmtree(os.path.join(new_dir_name, args.csp, ".terraform"))
    tf_zip = shutil.make_archive("citc-terraform", "gztar", ".", new_dir_name)
    if not args.dry_run:
        while call(["scp", "-i", key_path, "-o", "StrictHostKeyChecking no", "-o", "IdentitiesOnly=yes", tf_zip, "citc@{}:.".format(ip)]) != 0:
            print("Trying to upload Terraform state...")
            time.sleep(10)
    else:
        print("... pretending to upload the config {} to the cluster ...".format(tf_zip))
    os.remove(tf_zip)

    print("")
    print("#" * 80)
    print("")
    print("The file '{}' will allow you to log into the new cluster".format(key_path))
    print("Make sure you save this key as it is needed to destroy the cluster later.")
    print("")
    print("The IP address of the cluster is {}".format(ip))
    print("Connect with:")
    print("  ssh -i {ssh_id} citc@{ip}".format(ssh_id=key_path, ip=ip))
    print("")
    print("You can destroy the cluster with:")
    print("  python destroy-citc.py {csp} {ip} {ssh_id}".format(csp=args.csp, ip=ip, ssh_id=key_path))


def download_terraform(version):
    """Download Terraform binary and return its path"""

    if sys.platform.startswith("linux"):
        tf_platform = "linux_amd64"
    elif sys.platform == "darwin":
        tf_platform = "darwin_amd64"
    elif sys.platform == "win32":
        raise NotImplementedError("Windows is not supported at the moment")
    else:
        raise NotImplementedError("Platform {platform} is not supported".format(platform=sys.platform))

    tf_template = "https://releases.hashicorp.com/terraform/{v}/terraform_{v}_{p}.zip"
    tf_url = tf_template.format(v=version, p=tf_platform)
    print("Downloading Terraform binary")
    tf_zip, _ = urlretrieve(tf_url)
    ZipFile(tf_zip).extractall()
    os.chmod("terraform", stat.S_IRWXU)
    return "./terraform"


def config_file(csp, args):
    with open(os.path.join(csp, "terraform.tfvars")) as f:
        config = f.read()

    if csp == "aws":
        config = aws_config_file(config, args)
    else:
        raise NotImplementedError("Other providers are not supported yet")

    if args.ansible_repo:
        config = config + '\nansible_repo = "{}"'.format(args.ansible_repo)
    if args.ansible_branch:
        config = config + '\nansible_branch = "{}"'.format(args.ansible_branch)

    with open(os.path.join(csp, "terraform.tfvars"), "w") as f:
        f.write(config)


def aws_config_file(config, args):
    config = config.replace("~/.ssh/aws-key", "citc-key")
    with open("citc-key.pub") as pub_key:
        pub_key_text = pub_key.read().strip()
    config = config.replace("admin_public_keys = <<EOF", "admin_public_keys = <<EOF\n" + pub_key_text)
    if args.region:
        config += '\nregion = "{}"'.format(args.region)
    if args.availability_zone:
        config += '\navailability_zone = "{}"'.format(args.availability_zone)
    if args.profile:
        config += '\nprofile = "{}"'.format(args.profile)
    return config


if __name__ == "__main__":
    main()
