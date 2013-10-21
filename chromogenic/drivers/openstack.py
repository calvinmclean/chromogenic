"""
ImageManager:
    Remote Openstack Image management (Keystone, Nova, & Glance)

from chromogenic.drivers.openstack import ImageManager

credentials = {
    'username': '',
    'tenant_name': '',
    'password': '',
    'auth_url':'',
    'region_name':''
}
manager = ImageManager(**credentials)

manager.create_image('75fdfca4-d49d-4b2d-b919-a3297bc6d7ae', 'my new name')

"""
import os
import time
from pytz import datetime
from threepio import logger

from rtwo.provider import OSProvider
from rtwo.identity import OSIdentity
from rtwo.driver import OSDriver
from rtwo.drivers.common import _connect_to_keystone, _connect_to_nova,\
                                   _connect_to_glance, find

from service.deploy import freeze_instance, sync_instance
from service.tasks.driver import deploy_to
from chromogenic.drivers.base import BaseDriver
from chromogenic.common import run_command, wildcard_remove
from chromogenic.clean import remove_user_data, remove_atmo_data,\
                                  remove_vm_specific_data
from chromogenic.common import unmount_image, mount_image, remove_files,\
                                    fsck_qcow, get_latest_ramdisk
from keystoneclient.exceptions import NotFound

class ImageManager(BaseDriver):
    """
    Convienence class that uses a combination of boto and euca2ools calls
    to remotely download an image from the cloud
    * See http://www.iplantcollaborative.org/Zku
      For more information on image management
    """
    glance = None
    nova = None
    keystone = None

    @classmethod
    def lc_driver_init(self, lc_driver, *args, **kwargs):
        lc_driver_args = {
            'username': lc_driver.key,
            'password': lc_driver.secret,
            'tenant_name': lc_driver._ex_tenant_name,
            'auth_url': lc_driver._ex_force_auth_url,
            'region_name': lc_driver._ex_force_service_region
        }
        lc_driver_args.update(kwargs)
        manager = ImageManager(*args, **lc_driver_args)
        return manager

    @classmethod
    def _build_image_creds(cls, credentials):
        """
        Credentials - dict()

        return the credentials required to build an "ImageManager"
        """
        img_args = credentials.copy()
        #Required:
        img_args['key']
        img_args['secret']
        img_args['ex_tenant_name']
        img_args['ex_project_name']
        img_args['auth_url']
        img_args['region_name']
        img_args['admin_url']
        #Ignored:
        #img_args.pop('admin_url', None)
        #img_args.pop('router_name', None)
        #img_args.pop('ex_project_name', None)

        return img_args

    @classmethod
    def _image_creds_convert(cls, *args, **kwargs):
        creds = kwargs.copy()
        key = creds.pop('key', None)
        secret = creds.pop('secret', None)
        tenant = creds.pop('ex_tenant_name', None)
        creds.pop('ex_project_name', None)
        creds.pop('router_name', None)
        creds.pop('admin_url', None)
        if key and not creds.get('username'):
            creds['username'] = key
        if secret and not creds.get('password'):
            creds['password'] = secret
        if tenant and not creds.get('tenant_name'):
            creds['tenant_name'] = tenant
        return creds

    def __init__(self, *args, **kwargs):
        if len(args) == 0 and len(kwargs) == 0:
            raise KeyError("Credentials missing in __init__. ")

        self.admin_driver = self._build_admin_driver(**kwargs)
        creds = self._image_creds_convert(*args, **kwargs)
        (self.keystone,\
            self.nova,\
            self.glance) = self._new_connection(*args, **creds)

    def _parse_download_location(self, server, **kwargs):
        download_location = kwargs.get('download_location')
        download_dir = kwargs.get('download_dir')
        if not download_dir and not download_location:
            raise Exception("Could not parse download location. Expected "
                            "'download_dir' or 'download_location'")
        elif not download_location:
            #Use download dir & tenant_name to keep filesystem order
            tenant = find(self.keystone.tenants, id=server.tenant_id)
            local_user_dir = os.path.join(download_dir, tenant.name)
            if not os.path.exists(local_user_dir):
                os.makedirs(local_user_dir)
            download_location = os.path.join(local_user_dir, '%s.qcow2' % image_name)
        elif not download_dir:
            download_dir = os.path.dirname(download_location)
        return download_dir, download_location

    def parse_download_args(self, instance_id, **kwargs):
        #Step 0: Is the instance alive?
        server = self.get_server(instance_id)
        if not server:
            raise Exception("Instance %s does not exist" % instance_id)


        #Set download location
        download_dir, download_location = self._parse_download_location(server, **kwargs)
        download_args = {
                'snapshot_id': kwargs.get('snapshot_id'),
                'instance_id': instance_id, 
                'download_dir' : download_dir,
                'download_location' : download_location,
        }

    def download_instance(self, instance_id, download_location='/tmp', **kwargs):
        snapshot_id=kwargs.get('snapshot_id',None)
        if snapshot_id:
            snapshot = self.download_snapshot(snapshot_id, download_location)
        else:
            snapshot = self._download_instance(instance_id, download_location)
        fsck_qcow(download_location) # Maintain image consistency..
        return snapshot

    def create_image(self, instance_id, image_name, *args, **kwargs):
        """
        Creates an image of a running instance
        Required Args:
            instance_id - The instance that will be imaged
            image_name - The name of the image
            download_location OR download_dir - Where to download the image
            if download_dir:
                download_location = download_dir/username/image_name.qcow2
        """
        #Step 1: Retrieve a copy of the instance ( Use snapshot_id if given )
        download_kwargs = self.parse_download_args(instance_id, **kwargs)
        snapshot = self.download_instance(instance_id, **download_kwargs)

        #Step 2: Clean the local copy
        if kwargs.get('clean_image',True):
            self.mount_and_clean(
                    download_location,
                    os.path.join(download_dir, 'mount/'),
                    **kwargs)

        #Step 3: Upload the local copy as a 'real' image
        # with seperate kernel & ramdisk
        upload_args = self.parse_upload_args(image_name, download_location,
                                             kernel_id=snapshot.properties['kernel_id'],
                                             ramdisk_id=snapshot.properties['ramdisk_id'],
                                             **kwargs)

        new_image = self.upload_local_image(**upload_args)

        #Step 4: Cleanup after yourself
        if not kwargs.get('keep_image',False):
            snapshot.delete()
            wildcard_remove(download_dir)

        return new_image.id

    def parse_upload_args(self, image_name, download_location, **kwargs):
        """
        Use this function when converting 'create_image' args to
        'upload_local_image' args
        """
        if kwargs.get('kernel_id') and kwargs.get('ramdisk_id'):
            #Both kernel_id && ramdisk_id
            #Prepare for upload_local_image()
            return self._parse_args_upload_local_image(image_name,
                                                  download_location,
                                                  **kwargs)
        elif kwargs.get('kernel_path') and kwargs.get('ramdisk_path'):
            #Both kernel_path && ramdisk_path
            #Prepare for upload_full_image()
            return self._parse_args_upload_full_image(image_name,
                    download_location, **kwargs)
        #one path and one id OR no path no id
        else:
            raise Exception ("Cannot create upload arguments without either:"
                             " 1. kernel_id + ramdisk_id OR"
                             " 2. kernel_path + ramdisk_path")

    def _parse_args_upload_full_image(self, image_name,
                                       download_location, **kwargs)
        upload_args = {
            'image_name':image_name, 
            'image_file':download_location,
            'kernel_file':kwargs['kernel_path'], 
            'ramdisk_file':kwargs['ramdisk_path'], 
            'is_public':kwargs.get('public',True)
        }
        return upload_args

    def _parse_args_upload_local_image(self, image_name,
                                       download_location, **kwargs)
        upload_args = {
             'image_location':download_location,
             'image_name':image_name,
             'container_format':'ami',
             'disk_format':'ami',
             'is_public':kwargs.get('public', True), 
             'private_user_list':kwargs.get('private_user_list', []), 
             'properties':{
                 'kernel_id' :  kwargs['kernel_id'],
                 'ramdisk_id' : kwargs['ramdisk_id']
             }
        }
        return upload_args

    def download_snapshot(self, snapshot_id, download_location, *args, **kwargs):
        """
        Download an existing snapshot to local download directory
        Required Args:
            snapshot_id - The snapshot ID to be downloaded (1234-4321-1234)
            download_location - The exact path where image will be downloaded
        """
        #Step 1: Find snapshot by id
        return self.download_image(snapshot_id, download_location)


    def _download_instance(self, instance_id, download_location, *args, **kwargs):
        """
        Download an existing instance to local download directory
        Required Args:
            instance_id - The instance ID to be downloaded (1234-4321-1234)
            download_location - The exact path where image will be downloaded

        NOTE: It is recommended that you 'prepare the snapshot' before creating
        an image by running 'sync' and 'freeze' on the instance. See
        http://docs.openstack.org/grizzly/openstack-ops/content/snapsnots.html#consistent_snapshots
        """

        #Step 2: Create local path for copying image
        tenant = find(self.keystone.tenants, id=server.tenant_id)
        now = datetime.datetime.now() # Pytz datetime
        now_str = now.strftime('%Y-%m-%d_%H:%M:%S')
        ss_name = 'ChromoSnapShot_%s_%s' % (instance_id, now_str)
        meta_data = {}
        snapshot = self.create_snapshot(instance_id, ss_name, delay=True, **meta_data)
        return self.download_image(snapshot.id, download_location)

    def download_image(self, image_id, download_location):
        image = self.glance.images.get(image_id)
        #Step 2: Download local copy of snapshot
        logger.debug("Image downloading to %s" % download_location)
        with open(download_location,'w') as f:
            for chunk in image.data():
                f.write(chunk)
        logger.debug("Image downloaded to %s" % download_location)
        return image

    def upload_local_image(self, image_location, image_name,
                     container_format='ovf',
                     disk_format='raw',
                     is_public=True, private_user_list=[], properties={}):
        """
        Upload a single file as a glance image
        Defaults ovf/raw are correct for a eucalyptus .img file
        """
        new_image = self.glance.images.create(name=name,
                                             container_format=container_format,
                                             disk_format=disk_format,
                                             is_public=is_public,
                                             properties=properties,
                                             data=open(image_location))
        #TODO: For username in private_user_list
        #    share_image(new_meta,username)
        return new_image

    def upload_full_image(self, image_name, image_file,
                          kernel_file, ramdisk_file, is_public=True):
        """
        Upload a full image to glance..
            name - Name of image when uploaded to OpenStack
            image_file - Path containing the image file
            kernel_file - Path containing the kernel file
            ramdisk_file - Path containing the ramdisk file
        Requires 3 separate filepaths to uploads the Ramdisk, Kernel, and Image
        This is useful for migrating from Eucalyptus/AWS --> Openstack
        """
        new_kernel = self.upload_local_image(kernel_file,
                                             'eki-%s' % image_name,
                                             container_format='aki', 
                                             disk_format='aki', 
                                             is_public=is_public)
        new_ramdisk = self.upload_local_image(ramdisk_file,
                                             'eri-%s' % image_name,
                                             container_format='ari', 
                                             disk_format='ari', 
                                             is_public=is_public)
        opts = {
            'kernel_id' : new_kernel.id
            'ramdisk_id' : new_ramdisk.id
        }
        new_image = self.upload_local_image(image_file, image_name, 
                                             container_format='ami', 
                                             disk_format='ami', 
                                             is_public=is_public,
                                             properties=opts)
        return new_image

    def delete_images(self, image_id=None, image_name=None):
        if not image_id and not image_name:
            raise Exception("delete_image expects image_name or image_id as keyword"
            " argument")

        if image_name:
            images = [img for img in self.list_images()
                      if image_name in img.name]
        else:
            images = [self.glance.images.get(image_id)]

        if len(images) == 0:
            return False
        for image in images:
            self.glance.images.delete(image)

        return True

    # Public methods that are OPENSTACK specific

    def create_snapshot(self, instance_id, name, delay=False, **kwargs):
        """
        NOTE: It is recommended that you 'prepare the snapshot' before creating
        an image by running 'sync' and 'freeze' on the instance. See
        http://docs.openstack.org/grizzly/openstack-ops/content/snapsnots.html#consistent_snapshots
        """
        metadata = kwargs
        server = self.get_server(instance_id)
        if not server:
            raise Exception("Server %s does not exist" % instance_id)
        logger.debug("Instance is prepared to create a snapshot")
        snapshot_id = self.nova.servers.create_image(server, name, metadata)
        snapshot = self.get_image(snapshot_id)
        if not delay:
            return snapshot
        #Step 2: Wait (Exponentially) until status moves from:
        # queued --> saving --> active
        attempts = 0
        while True:
            snapshot = self.get_image(snapshot_id)
            if attempts >= 40:
                break
            if snapshot.status == 'active':
                break
            attempts += 1
            logger.debug("Snapshot %s in non-active state %s" % (snapshot_id, snapshot.status))
            logger.debug("Attempt:%s, wait 1 minute" % attempts)
            time.sleep(60)
        if snapshot.status != 'active':
            raise Exception("Create_snapshot timeout. Operation exceeded 40m")
        return snapshot


    # Private methods and helpers
    def _read_file_type(self, local_image):
        out, _ = run_command(['file', local_image])
        logger.info("FileOutput: %s" % out)
        if 'qemu qcow' in out.lower():
            if 'v2' in out.lower():
                return 'qcow2'
            else:
                return 'qcow'
        elif 'Linux rev 1.0' in out.lower() and 'ext' in out.lower():
            return 'img'
        else:
            raise Exception("Could not guess the type of file. Output=%s"
                            % out)


    def _admin_identity_creds(self, **kwargs):
        creds = {}
        creds['key'] = kwargs.get('key')
        creds['secret'] = kwargs.get('secret')
        creds['ex_tenant_name'] = kwargs.get('ex_tenant_name')
        creds['ex_project_name'] = kwargs.get('ex_project_name')
        return creds

    def _admin_driver_creds(self, **kwargs):
        creds = {}
        creds['region_name'] = kwargs.get('region_name')
        creds['router_name'] = kwargs.get('router_name')
        creds['admin_url'] = kwargs.get('admin_url')
        creds['ex_force_auth_url'] = kwargs.get('auth_url')
        return creds

    def _build_admin_driver(self, **kwargs):
        #Set Meta
        OSProvider.set_meta()
        #TODO: Set location from kwargs
        provider = OSProvider(identifier=kwargs.get('location'))
        admin_creds = self._admin_identity_creds(**kwargs)
        logger.info("ADMINID Creds:%s" % admin_creds)
        identity = OSIdentity(provider, **admin_creds)
        driver_creds = self._admin_driver_creds(**kwargs)
        logger.info("ADMINDriver Creds:%s" % driver_creds)
        admin_driver = OSDriver(provider, identity, **driver_creds)
        return admin_driver

    def _new_connection(self, *args, **kwargs):
        """
        Can be used to establish a new connection for all clients
        """
        keystone = _connect_to_keystone(*args, **kwargs)
        nova = _connect_to_nova(*args, **kwargs)
        glance = _connect_to_glance(keystone, *args, **kwargs)
        return (keystone, nova, glance)

    def get_instance(self, instance_id):
        instances = self.admin_driver._connection.ex_list_all_instances()
        for inst in instances:
            if inst.id == instance_id:
                return inst
        return None

    def get_server(self, server_id):
        servers = [server for server in
                self.nova.servers.list(search_opts={'all_tenants':1}) if
                server.id == server_id]
        if not servers:
            return None
        return servers[0]

    def list_images(self):
        return self.nova.images.list()

    def get_image_by_name(self, name):
        for img in self.glance.images.list():
            if img.name == name:
                return img
        return None

    #Image sharing
    def shared_images_for(self, tenant_name=None, image_name=None):
        """

        @param can_share
        @type Str
        If True, allow that tenant to share image with others
        """
        if tenant_name:
            tenant = self.find_tenant(tenant_name)
            return self.glance.image_members.list(member=tenant)
        if image_name:
            image = self.find_image(image_name)
            return self.glance.image_members.list(image=image)

    def share_image(self, image, tenant_id, can_share=False):
        """

        @param can_share
        @type Str
        If True, allow that tenant to share image with others
        """
        return self.glance.image_members.create(
                    image, tenant_id, can_share=can_share)

    def unshare_image(self, image, tenant_id):
        tenant = find(self.keystone.tenants, name=tenant_name)
        return self.glance.image_members.delete(image.id, tenant.id)

    #Alternative image uploading

    #Lists
    def admin_list_images(self):
        """
        These images have an update() function
        to update attributes like public/private, min_disk, min_ram

        NOTE: glance.images.list() returns a generator, we return lists
        """
        return [i for i in self.glance.images.list()]

    def list_images(self):
        return [img for img in self.glance.images.list()]

    #Finds
    def get_image(self, image_id):
        found_images = [i for i in self.glance.images.list() if
                i.id == image_id]
        if not found_images:
            return None
        return found_images[0]

    def find_image(self, image_name, contains=False):
        return [i for i in self.glance.images.list() if
                i.name == image_name or
                (contains and image_name in i.name)]

    def find_tenant(self, tenant_name):
        try:
            tenant = find(self.keystone.tenants, name=tenant_name)
            return tenant
        except NotFound:
            return None
