#!/usr/bin/env python

# Copyright 2017 Bluecat Networks Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

# BlueCat Neutron Monitor listens on the AMQP message bus of Openstack. 
# Whenever a Neutron notification message is seen for a port or floating_IP update, does X
# B.Shorland - Bluecat Networks 2017

import dns.name
import dns.message
import dns.query
import dns.flags
import dns.reversename
import dns.rdatatype
import dns.update
import dns.resolver
import dns.exception
import string
import ipaddress
import datetime
import sys, optparse
import json
import logging as log
from kombu import BrokerConnection
from kombu import Exchange
from kombu import Queue
from kombu.mixins import ConsumerMixin

version = 0.3
EXCHANGE_NAME="neutron"
ROUTING_KEY="notifications.info"
QUEUE_NAME="bluecat_neutron_monitor"
BROKER_URI="amqp://guest:guest@localhost:5672//"
FLOAT_START="floatingip.create.start"
FLOAT_END="floatingip.create.end"
FLOAT_U_START="floatingip.update.start"
FLOAT_U_END="floatingip.update.end"
PORT_START="port.create.start"
PORT_END="port.create.end"
PORT_U_START="port.update.start"
PORT_U_END="port.update.end"
ADDITIONAL_RDCLASS = 65535

# Parse command line arguments
parser = optparse.OptionParser()
parser.add_option('-n','--nameserver',dest="nameserver",default="0.0.0.0",)
parser.add_option('-l','--logfile',dest="logfile",default="/opt/stack/devstack/bluecat/bluecat_neutron.log",)
parser.add_option('-t','--ttl',dest="ttl",type=int,default=1,)
parser.add_option('-d','--domain',dest="domain",default=False,)
parser.add_option('-r','--replace',dest="replace",default=False,)
options, remainder = parser.parse_args()
print 'Sending DDNS Updates to BDDS = ',options.nameserver
print 'Debug Logging = ',options.logfile
print 'DDNS TTL = ',options.ttl
print 'Domain = ',options.domain
print 'Replace FixedIP with Floating = ',options.replace

# Set INFO to DEBUG to see the RabbitMQ BODY messages 
log.basicConfig(filename=options.logfile, level=log.INFO, format='%(asctime)s %(message)s')

def stripptr(substr, str):
        index = 0
        length = len(substr)
        while string.find(str,substr) != -1:
                index = string.find(str,substr)
                str = str[0:index] + str[index+length:]
	str = str.rstrip('.')
        return str

# Check the reverse zone authority upon target DNS server
def getrevzone_auth(domain):
	domain = dns.name.from_text(domain)
	if not domain.is_absolute():
		domain = domain.concatenate(dns.name.root)
	request = dns.message.make_query(domain, dns.rdatatype.ANY)
	request.flags |= dns.flags.AD
	request.find_rrset(request.additional, dns.name.root, ADDITIONAL_RDCLASS, dns.rdatatype.OPT, create=True, force_unique=True)
	response = dns.query.udp(request, options.nameserver)
	if not response.authority:
		log.info ('[getrevzone_auth] - DNS not authoritive')
		return
	else:
		auth_reverse = str(response.authority).split(' ')[1]
		log.info ('[getrezone_auth] - %s' % str(auth_reverse).lower())
		return str(auth_reverse).lower()

# Add PTR record for a given address
def addREV(ipaddress,ttl,name):
	reversedomain = dns.reversename.from_address(str(ipaddress))
	reversedomain = str(reversedomain).rstrip('.')
	log.info ('[addREV] - reversedomain  %s' % reversedomain)
	authdomain = getrevzone_auth(str(reversedomain)).rstrip('.')
	log.info ('[addREV] - authdomain %s' % authdomain)
	label = stripptr(authdomain, reversedomain)
	log.info ('[addREV] - label %s' % label)
	log.info ('[addREV] - name %s' % name)
	update = dns.update.Update(authdomain)
	if options.replace == False:
		update.add(label,options.ttl,dns.rdatatype.PTR, name)
	else: 
		update.replace(label,options.ttl,dns.rdatatype.PTR, name)
	response = dns.query.udp(update, options.nameserver)
	return response
	
# Delete PTR record for a passed address
def delREV(ipaddress,name):
	name = str(name)
	reversedomain = dns.reversename.from_address(str(ipaddress))
	reversedomain = str(reversedomain).rstrip('.')
	log.info ('[delREV] - reversedomain  %s' % reversedomain)
	authdomain = getrevzone_auth(str(reversedomain)).rstrip('.')
	log.info ('[delREV] - authdomain  %s' % authdomain)
	update = dns.update.Update(authdomain)
	label = stripptr(authdomain, reversedomain)
	log.info ('[delREV] - label  %s' % label)
	update.delete(label,'PTR',name)
	response = dns.query.udp(update, options.nameserver)
	return response
	
# Delete A/AAAA record from name
def delFWD(name,ipaddress):
	name = str(name)
	ipaddress = str(ipaddress)
	update = dns.update.Update(splitFQDN(name)[1])
	hostname = splitFQDN(name)[0]
	domain = splitFQDN(name)[1]
	log.info ('[delFWD] - name %s' % name)
	log.info ('[delFWD] - ipaddress %s' % ipaddress)
	log.info ('[delFWD] - hostname %s' % hostname)
	log.info ('[delFWD] - domainname %s' % domain)
	update.delete(hostname, 'A', ipaddress)
	response = dns.query.udp(update, options.nameserver)
	return response

# add A/AAAA record 
def addFWD(name,ttl,ipaddress):
	ipaddress = str(ipaddress)
	update = dns.update.Update(splitFQDN(name)[1])
	hostname = splitFQDN(name)[0]
	log.info ('[addFWD] - hostname %s' % hostname)
	log.info ('[addFWD] - domain %s' % splitFQDN(name)[1])
	address_type = enumIPtype(ipaddress)
        if address_type == 4:
		log.info ('[addFWD] - IPv4') 
		if options.replace == False:
			update.add(hostname,options.ttl,dns.rdatatype.A, ipaddress)
		else: 
			update.replace(hostname,options.ttl,dns.rdatatype.A, ipaddress)
	elif address_type == 6:
		log.info ('[addFWD] - IPv6')
		if options.replace == False:
			update.add(hostname,options.ttl,dns.rdatatype.AAAA, ipaddress)
		else: 
			update.replace(hostname,options.ttl,dns.rdatatype.AAAA, ipaddress)
	response = dns.query.udp(update, options.nameserver)
	return response

# Resolve PTR record from either IPv4 or IPv6 address 
def resolvePTR(address):
	type = enumIPtype(address)
	address = str(address)
	if type == 4:
		req = '.'.join(reversed(address.split('.'))) + ".in-addr.arpa."
		log.info ('[ResolvePTR] - %s' % req)
	elif type == 6:
		# exploded concatenated V6 address out
		v6address = ipaddress.ip_address(unicode(address))
		v6address = v6address.exploded
		req = '.'.join(reversed(v6address.replace(':',''))) + ".ip6.arpa."
		log.info ('[ResolvePTR] - %s' % req)
	myResolver = dns.resolver.Resolver()
	myResolver.nameservers = [options.nameserver]
	try:
		myAnswers = myResolver.query(req, "PTR")
		for rdata in myAnswers:
			log.info ('[ResolvePTR] - %s' % rdata)
			return rdata
	except:
		log.info ('[ResolvePTR] - PTR query failed')
		return "PTR Query failed"

# Returns address type 4 or 6 
def enumIPtype(address):
	address = ipaddress.ip_address(unicode(address))
	return address.version

# Splits FQDN into host and domain portions
def splitFQDN(name):
	hostname = name.split('.')[0]
	domain = name.partition('.')[2]
	return hostname,domain

class BCUpdater(ConsumerMixin):
    
    def __init__(self, connection):
        self.connection = connection
        return

    def get_consumers(self, consumer, channel):
        exchange = Exchange(EXCHANGE_NAME, type='topic', durable=False)
        queue = Queue(
            QUEUE_NAME,
            exchange,
            routing_key=ROUTING_KEY,
            durable=False,
            auto_delete=True,
            no_ack=True,
            )
        return [consumer(queue, callbacks=[self.on_message])]

    def on_message(self, body, message):
        try:
            self._handle_message(body)
        except Exception, e:
            log.info(repr(e))

# Message handler extracts event_type
    def _handle_message(self, body):
		log.debug('Body: %r' % body)
		jbody = json.loads(body['oslo.message'])
		event_type = jbody['event_type']
		log.info ('EVENT_TYPE = %s' % event_type) 
 		if event_type == FLOAT_START:
 			# no relevent information in floatingip.create.start
 			log.info ('[floatingip.create.start]') 
 			
 		elif event_type == FLOAT_END:
 			# only floating_ip_address in payload as IP is selected from pool
 			fixed = jbody['payload']['floatingip']['fixed_ip_address']
 			log.info ('[floatingip.create.end] -> FIXED_IP_ADDRESS = %s' % fixed) 
 			float = jbody['payload']['floatingip']['floating_ip_address']
 			log.info ('[floatingip.create.end] -> FLOATING_IP_ADDRESS = %s' % float) 
 			port_id = jbody['payload']['floatingip']['port_id']
 			log.info ('[floatingip.create.end] -> PORT_ID = %s' % port_id)
 				
 		elif event_type == FLOAT_U_START:
 			# fixed IP from instance to which floating IP will be assigned and the port_id (upon associated)
 			# NULL (upon dis-associated)
 			if 'fixed_ip_address' in jbody['payload']['floatingip']:
 				fixed = jbody['payload']['floatingip']['fixed_ip_address']
 				if fixed is not None:
 					log.info ('[floatingip.update.start] -> FIXED_IP_ADDRESS = %s' % fixed) 
 					checkit = resolvePTR(fixed)
 					log.info ('[floatingip.update.start] -> FIXED FQDN = %s' % checkit)
 					port_id = jbody['payload']['floatingip']['port_id']
 					log.info ('[floatingip.update.start] -> PORT_ID = %s' % port_id) 
 			
 		elif event_type == FLOAT_U_END:
 			# Fixed_IP, Floating_IP and Port_ID seen (upon associate)
 			# Fixed_IP = None, floating_IP, and port_id = None (upon disassociation)
 			if 'fixed_ip_address' in jbody['payload']['floatingip']:
 				fixed = jbody['payload']['floatingip']['fixed_ip_address']
 				log.info ('[floatingip.update.end] -> FixedIP = %s ' % fixed)
 				float = jbody['payload']['floatingip']['floating_ip_address']
 				log.info ('[floatingip.update.end] -> FloatingIP = %s ' % float)
 				port_id = jbody['payload']['floatingip']['port_id']
 				log.info ('[floatingip.update.end] -> PortID = %s' % port_id)
 				if fixed is not None and float is not None and port_id is not None: 
 					log.info ('[floatingip.update.end] -> Associating FloatingIP to instance')
 					log.info ('[floatingip.update.end] -> FIXED_IP_ADDRESS = %s' % fixed) 
 					checkit = str(resolvePTR(fixed))
 					log.info ('[floatingip.update.end] -> FIXED FQDN = %s' % checkit)
 					log.info ('[floatingip.update.end] -> FLOATING_IP_ADDRESS = %s' % float) 
 					log.info ('[floatingip.update.end] -> PORT_ID = %s' % port_id) 
 					if options.replace == False:
						log.info ('[floatingip.update.end] - Updating DNS - adding FLOATING_IP records')
					else: 
						log.info ('[floatingip.update.end] - Updating DNS - replacing FIXED_IP records with FLOATING_IP')
 					addFWD(checkit,'666',float)
 					addREV(float,'666',checkit)
 				elif fixed is None and float and port_id is None:
 					log.info ('[floatingip.update.end] -> disassociating FloatingIP from instance')
 					checkit = str(resolvePTR(float))
 					log.info ('[floatingip.update.end] -> FLOATING_IP_ADDRESS = %s' % float)
 					log.info ('[floatingip.update.end] -> FLOATING_IP FQDN = %s' % checkit)
 					log.info ('[floatingip.update.end] - removing FLOATING_IP records')
 					delFWD(checkit,float)
 					delREV(float,checkit)	
 			
 		elif event_type == PORT_START:
 			if 'id' in jbody['payload']['port']:
 				port_id = jbody['payload']['port']['id']
 				log.info ('[port.create.start] -> PORT_ID = %s' % port_id) 
 			
 		elif event_type == PORT_END:
 			port_id = jbody['payload']['port']['id']
 			log.info ('[port.create.end] -> PORT_ID = %s' % port_id) 
 			
 		elif event_type == PORT_U_START:
 			if 'id' in jbody['payload']['port']:
 				port_id = jbody['payload']['port']['id']
 				log.info ('[port.update.start] - > PORT_ID = %s' % port_id) 
 			
 		elif event_type == PORT_U_END:
 			port_id = jbody['payload']['port']['id']
 			log.info ('[port.update.end] -> PORT_ID = %s' % port_id) 
 			for temp in jbody['payload']['port']['fixed_ips']:
 				addr = temp['ip_address']
 				log.info ('[port.update.end] -> IP ADDRESS = %s' % addr) 
 					
 		
if __name__ == "__main__":
	log.info("BlueCat Neutron Monitor - %s Bluecat Networks 2017" % version)
	log.info("- Sending RFC2136 Dynamic DNS updates to DNS: %s" % options.nameserver)
	log.info("- Debugging Logging to %s" % options.logfile)
	log.info("- Dynamic TTL for Records: %s" % options.ttl)
	log.info("- Override Domain: %s" % options.domain)
	log.info("- Replace FixedIP with Floating: %s" % options.replace)
	
	with BrokerConnection(BROKER_URI) as connection:
		try:
			print(connection)
			BCUpdater(connection).run()
		except KeyboardInterrupt:
			print(' - Exiting Bluecat Neutron Monitor ....')

