#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lstm_controller.py: pytorch module implementing NTM interface to external memory."""
__author__ = "Tomasz Kornuta"


import torch 
import torch.nn.functional as F
import collections
import numpy as np
import logging
logger = logging.getLogger('NTM-Interface')

# Helper collection type.
_InterfaceStateTuple = collections.namedtuple('InterfaceStateTuple', ('read_weights', 'write_weights'))

class InterfaceStateTuple(_InterfaceStateTuple):
    """Tuple used by interface for storing current/past state information"""
    __slots__ = ()


class Interface(torch.nn.Module):
    """Class realizing interface between controller and memory.
    """
    def __init__(self, params):
        """ Constructor.
        
        :param params: Dictionary of parameters.
        """
        # Call constructor of base class.
        super(Interface, self).__init__() 

        # Parse parameters.
        # Get hidden state size.
        self.ctrl_hidden_state_size = params['controller']['hidden_state_size']
        # Get memory parameters.
        self.num_memory_content_bits = params['memory']['num_content_bits']
        # Get interface parameters.
        self.interface_shift_size = params['interface']['shift_size']
        assert self.interface_shift_size % 2 != 0,  'Shift size must be an odd number'
        assert self.interface_shift_size >0,  'Shift size must be > 0'
        self.interface_num_read_heads = params['interface']['num_read_heads']
        assert self.interface_num_read_heads >= 1, "NTM requires at least 1 read head (currently %r)" % self.interface_num_read_heads     

 
        # -------------- READ HEADS -----------------#
        # Number/size of parameters of a single read head: key [MEMORY_CONTENT_BITS] + beta [1] + gate [1] + gamma [1] + shift kernel size [SHIFT_SIZE]
        # All read params = NUM_HEADS * above (but it's not important here)
        num_read_params =  (self.num_memory_content_bits +1 +1 +1 +self.interface_shift_size)
        # Dictionary with read parameters - used during slicing.
        self.read_param_locations = self.calculate_param_locations({'query_vector': self.num_memory_content_bits, 'beta': 1, 'gate': 1,  
            'shift': self.interface_shift_size, 'gamma': 1},  "Read")
        assert num_read_params == self.read_param_locations[-1], "Last location must be equal to number of read params."
        
       # Forward linear layers that generate parameters of read heads.
        self.hidden2read_list = []
        for _ in range(self.interface_num_read_heads):
            self.hidden2read_list.append(torch.nn.Linear(self.ctrl_hidden_state_size,  num_read_params))
 
        # -------------- WRITE HEAD -----------------#
        # Number/size of wrrite parameters:  key [MEMORY_BITS] + beta [1] + gate [1] + gamma [1] + 
        # + shift kernel size [SHIFT_SIZE] + erase vector [MEMORY_CONTENT_BITS] + write vector[MEMORY_BITS]  
        num_write_params = 3*self.num_memory_content_bits +1 +1 +1 +self.interface_shift_size
        
        # Write parameters - used during slicing.
        self.write_param_locations = self.calculate_param_locations({'query_vector': self.num_memory_content_bits, 'beta': 1, 'gate': 1,  
            'shift': self.interface_shift_size, 'gamma': 1, 
            'erase_vector': self.num_memory_content_bits, 'add_vector': self.num_memory_content_bits}, "Write")
        assert num_write_params == self.write_param_locations[-1], "Last location must be equal to number of write params."
        
       # Forward linear layer that generates parameters of write heads.
        self.hidden2write_params = torch.nn.Linear(self.ctrl_hidden_state_size,  num_write_params)

    def init_state(self,  batch_size,  num_memory_addresses):
        """
        Returns 'zero' (initial) state tuple.
        
        :param batch_size: Size of the batch in given iteraction/epoch.
        :param num_memory_addresses: Number of memory addresses.
        :returns: Initial state tuple - object of InterfaceStateTuple class.
        """
        # Add read attention vectors - one for each read head.
        read_attentions = []
        for _ in range(self.interface_num_read_heads):
            # Read attention weights [BATCH_SIZE x MEMORY_ADDRESSES x 1]
            # Normalize through division by number of addresses.
            read_attentions.append(torch.ones((batch_size, num_memory_addresses,  1), dtype=torch.float)/num_memory_addresses)
        
        # Single write head - write attention weights [BATCH_SIZE x MEMORY_ADDRESSES x 1]
        # Normalize through division by number of addresses.
        write_attention = torch.ones((batch_size, num_memory_addresses,  1), dtype=torch.float)/num_memory_addresses

        return InterfaceStateTuple(read_attentions,  write_attention)

    def forward(self, ctrl_hidden_state_BxH,  prev_memory_BxAxC,  prev_state_tuple):
        """
        Controller forward function. 
        
        :param ctrl_hidden_state_BxH: a Tensor with controller hidden state of size [BATCH_SIZE  x HIDDEN_SIZE]
        :param prev_memory_BxAxC: Previous state of the memory [BATCH_SIZE x  MEMORY_ADDRESSES x CONTENT_BITS] 
        :param prev_state_tuple: Tuple containing previous read and write attention vectors.
        :returns: List of read vectors [BATCH_SIZE x CONTENT_SIZE], updated memory and state tuple (object of LSTMStateTuple class).
        """
        # Unpack previous cell  state - just to make sure that everything is ok...
        (prev_read_attentions_BxAx1_H,  prev_write_attention_BxAx1) = prev_state_tuple
        
        # !! Execute single step !!
        
        # Read attentions 
        read_attentions_BxAx1_H = []
        # List of read vectors - with two dimensions! [BATCH_SIZE x CONTENT_SIZE]
        read_vectors_BxC_H = []

        # Read heads.
        for i in range(self.interface_num_read_heads):
            # Calculate parameters of a given read head.
            params_BxP = self.hidden2read_list[i](ctrl_hidden_state_BxH)

            # Split the parameters.
            query_vector_BxC,  beta_Bx1,  gate_Bx1, shift_BxS, gamma_Bx1 = self.split_params(params_BxP,  self.read_param_locations)

            # Update the attention of a given read head.
            read_attention_BxAx1 = self.update_attention(query_vector_BxC,  beta_Bx1,  gate_Bx1, shift_BxS, gamma_Bx1,  prev_memory_BxAxC,  prev_read_attentions_BxAx1_H[i])
            logger.debug("read_attention_BxAx1 {}:\n {}".format(read_attention_BxAx1.size(),  read_attention_BxAx1))  

            # Read vector from memory [BATCH_SIZE x CONTENT_BITS x 1].
            read_vector_Bx1xC = torch.matmul(torch.transpose(read_attention_BxAx1,  1, 2),  prev_memory_BxAxC)
            logger.debug("read_vector_Bx1xC {}:\n {}".format(read_vector_Bx1xC.size(),  read_vector_Bx1xC))  
            
            # Save read attentions and vectors in a list.
            read_attentions_BxAx1_H.append(read_attention_BxAx1)
            read_vectors_BxC_H.append(read_vector_Bx1xC.squeeze(dim=1))
            
        # Write head operation.
        # Calculate parameters of a given read head.
        params_BxP = self.hidden2write_params(ctrl_hidden_state_BxH)

        # Split the parameters.
        query_vector_BxC,  beta_Bx1,  gate_Bx1, shift_BxS, gamma_Bx1,  erase_vector_BxC,  add_vector_BxC  = self.split_params(params_BxP,  self.write_param_locations)

        # Add 3rd dimensions where required and apply non-linear transformations.
        # I didn't had that non-linear transformation in TF!
        erase_vector_Bx1xC = F.sigmoid(erase_vector_BxC).unsqueeze(1) 
        add_vector_Bx1xC = F.sigmoid(add_vector_BxC).unsqueeze(1) 

        # Update the attention of the write head.
        write_attention_BxAx1 = self.update_attention(query_vector_BxC,  beta_Bx1,  gate_Bx1, shift_BxS, gamma_Bx1,  prev_memory_BxAxC,  prev_write_attention_BxAx1)
        logger.debug("write_attention_BxAx1 {}:\n {}".format(write_attention_BxAx1.size(),  write_attention_BxAx1))  

        # Update the memory.
        # 1. Calculate the preserved content.
        preserve_content_BxAxC = torch.ones_like(prev_memory_BxAxC) - torch.matmul(write_attention_BxAx1,  erase_vector_Bx1xC)
        # 2. Calculate the added content.
        add_content_BxAxC = torch.matmul(write_attention_BxAx1,  add_vector_Bx1xC) 
        # 3. Update.
        memory_BxAxC =  prev_memory_BxAxC * preserve_content_BxAxC + add_content_BxAxC        
        
        # Pack current cell state.
        state_tuple = InterfaceStateTuple(read_attentions_BxAx1_H,  write_attention_BxAx1)
        
        # Return read vector, new memory state and state tuple.
        return read_vectors_BxC_H, memory_BxAxC,  state_tuple
 
    def calculate_param_locations(self,  param_sizes_dict,  head_name):
        """ Calculates locations of parameters, that will subsequently be used during parameter splitting.
        :param param_sizes_dict: Dictionary containing parameters along with their sizes (in bits/units).
        :param head_name: Name of head.
        :returns: "Locations" of parameters.
        """
        logger.debug("{} param sizes dict:\n {}".format(head_name, param_sizes_dict))        
        # Create the parameter lengths and store their cumulative sum
        lengths = np.fromiter(param_sizes_dict.values(), dtype=int)
        # Store "parameter locations" for further usage.
        param_locations = np.cumsum(np.insert(lengths, 0, 0), dtype=int).tolist()
        logger.debug("{} param locations:\n {}".format(head_name, param_locations))          
        return param_locations
        
    def split_params(self,  params,  locations):
        """ Split parameters into list on the basis of locations."""
        param_splits = [params[..., locations[i]:locations[i+1]]  for i in range(len(locations)-1)]
        logger.debug("Splitted params:\n {}".format(param_splits)) 
        return param_splits

    def update_attention(self,  query_vector_BxC,  beta_Bx1,  gate_Bx1, shift_BxS, gamma_Bx1,  prev_memory_BxAxC,  prev_attention_BxAx1):
        """ Updates the attention weights.
        
        :param query_vector_BxC: Query used for similarity calculation in content-based addressing [BATCH_SIZE x CONTENT_BITS]
        :param beta_Bx1: Strength parameter used in content-based addressing.
        :param gate_Bx1:
        :param shift_BxS:
        :param gamma_Bx1:
        :param prev_memory_BxAxC: tensor containing memory before update [BATCH_SIZE x MEMORY_ADDRESSES x CONTENT_BITS]
        :param prev_attention_BxAx1: previous attention vector [BATCH_SIZE x MEMORY_ADDRESSES x 1]
        :returns: attention vector of size [BATCH_SIZE x ADDRESS_SIZE x 1]
        """
        # Add 3rd dimensions where required and apply non-linear transformations.
        # Produce content-addressing params.
        # Query/key: I didn't had that non-linear transformation in TF!
        query_vector_Bx1xC = F.sigmoid(query_vector_BxC).unsqueeze(1) 
        # Beta: oneplus
        beta_Bx1x1 = F.softplus(beta_Bx1).unsqueeze(2) +1
        # Produce gating param.
        gate_Bx1x1 = F.sigmoid(gate_Bx1).unsqueeze(2)
        # Produce location-addressing params.
        shift_BxSx1 = F.softmax(shift_BxS, dim=1).unsqueeze(2)
        # Truncate gamma to  range 1-50
        gamma_Bx1x1 =F.softplus(gamma_Bx1).unsqueeze(2) +1
       # torch.clamp(gamma_Bx1, 1, 50).unsqueeze(2)
        
        # Content-based addressing.
        content_attention_BxAx1 = self.content_based_addressing(query_vector_Bx1xC,  beta_Bx1x1,  prev_memory_BxAxC)
    
        # Gating mechanism - choose beetween new attention from CBA or attention from previous iteration. [BATCH_SIZE x ADDRESSES x 1].
        logger.debug("prev_attention_BxAx1 {}:\n {}".format(prev_attention_BxAx1.size(),  prev_attention_BxAx1))    
        
        attention_after_gating_BxAx1 = gate_Bx1x1 * content_attention_BxAx1  +(torch.ones_like(gate_Bx1x1) - gate_Bx1x1) * prev_attention_BxAx1
        logger.debug("attention_after_gating_BxAx1 {}:\n {}".format(attention_after_gating_BxAx1.size(),  attention_after_gating_BxAx1))    

        # Location-based addressing.
        location_attention_BxAx1 = self.location_based_addressing(attention_after_gating_BxAx1,  shift_BxSx1,  gamma_Bx1x1,  prev_memory_BxAxC)
        logger.debug("location_attention_BxAx1 {}:\n {}".format(location_attention_BxAx1.size(),  location_attention_BxAx1))    
        
        return location_attention_BxAx1
        
    def content_based_addressing(self,  query_vector_Bx1xC, beta_Bx1x1, prev_memory_BxAxC):
        """Computes content-based addressing. Uses query vectors for calculation of similarity.
        
        :param query_vector_Bx1xC: NTM "key"  [BATCH_SIZE x 1 x CONTENT_BITS] 
        :param beta_Bx1x1: key strength [BATCH_SIZE x 1 x 1]
        :param prev_memory_BxAxC: tensor containing memory before update [BATCH_SIZE x MEMORY_ADDRESSES x CONTENT_BITS]
        :returns: attention of size [BATCH_SIZE x ADDRESS_SIZE x 1]
        """
        # Normalize query batch - along content.
        norm_query_vector_Bx1xC = F.normalize(query_vector_Bx1xC, p=2,  dim=2)
        logger.debug("norm_query_vector_Bx1xC {}:\n {}".format(norm_query_vector_Bx1xC.size(),  norm_query_vector_Bx1xC))  

        # Normalize memory - along content. 
        norm_memory_BxAxC = F.normalize(prev_memory_BxAxC, p=2,  dim=2)
        logger.debug("norm_memory_BxAxC {}:\n {}".format(norm_memory_BxAxC.size(),  norm_memory_BxAxC))  
        
        # Calculate cosine similarity [BATCH_SIZE x MEMORY_ADDRESSES x 1].
        similarity_BxAx1 = torch.matmul(norm_memory_BxAxC,  torch.transpose(norm_query_vector_Bx1xC,  1, 2))
        logger.debug("similarity_BxAx1 {}:\n {}".format(similarity_BxAx1.size(),  similarity_BxAx1))    
        
        # Element-wise multiplication [BATCH_SIZE x MEMORY_ADDRESSES x 1]
        strengthtened_similarity_BxAx1 = torch.matmul(similarity_BxAx1,  beta_Bx1x1)
        logger.debug("strengthtened_similarity_BxAx1 {}:\n {}".format(strengthtened_similarity_BxAx1.size(),  strengthtened_similarity_BxAx1))    

        # Calculate attention based on similarity along the "slot dimension" [BATCH_SIZE x MEMORY_ADDRESSES x 1].
        attention_BxAx1 = F.softmax(strengthtened_similarity_BxAx1, dim=1)
        logger.debug("attention_BxAx1 {}:\n {}".format(attention_BxAx1.size(),  attention_BxAx1))    
        return attention_BxAx1

    def location_based_addressing(self,  attention_BxAx1,  shift_BxSx1,  gamma_Bx1x1,  prev_memory_BxAxC):
        """ Computes location-based addressing, i.e. shitfts the head and sharpens.
        
        :param attention_BxAx1: Current attention [BATCH_SIZE x ADDRESS_SIZE x 1]
        :param shift_BxSx1: soft shift maks (convolutional kernel) [BATCH_SIZE x SHIFT_SIZE x 1]
        :param gamma_Bx1x1: sharpening factor [BATCH_SIZE x 1 x 1]
        :param prev_memory_BxAxC: tensor containing memory before update [BATCH_SIZE x MEMORY_ADDRESSES x CONTENT_BITS]
        :returns: attention vector of size [BATCH_SIZE x ADDRESS_SIZE x 1]
        """

        # 1. Perform circular convolution.
        shifted_attention_BxAx1 = self.circular_convolution(attention_BxAx1,  shift_BxSx1,  prev_memory_BxAxC)
        
        # 2. Perform Sharpening.
        sharpened_attention_BxAx1 = self.sharpening(shifted_attention_BxAx1,  gamma_Bx1x1)
               
        return sharpened_attention_BxAx1

    def circular_convolution(self,  attention_BxAx1,  shift_BxSx1,  prev_memory_BxAxC):
        """ Performs circular convoution, i.e. shitfts the attention accodring to given shift vector (convolution mask).
        
        :param attention_BxAx1: Current attention [BATCH_SIZE x ADDRESS_SIZE x 1]
        :param shift_BxSx1: soft shift maks (convolutional kernel) [BATCH_SIZE x SHIFT_SIZE x 1]
        :param prev_memory_BxAxC: tensor containing memory before update [BATCH_SIZE x MEMORY_ADDRESSES x CONTENT_BITS]
        :returns: attention vector of size [BATCH_SIZE x ADDRESS_SIZE x 1]
        """
        def circular_index(idx, num_addr):
            """ Calculates the index, taking into consideration the number of addresses in memory.
            :param idx: index (single element)
            :param num_addr: number of addresses in memory
            """
            if idx < 0: return num_addr + idx
            elif idx >= num_addr : return idx - num_addr
            else: return idx

        # Get number of memory addresses and batch size.
        batch_size =prev_memory_BxAxC.size(0) 
        num_addr = prev_memory_BxAxC.size(1)
        shift_size = self.interface_shift_size
        
        logger.debug("shift_BxSx1 {}: {}".format(shift_BxSx1,  shift_BxSx1.size()))    
        # Create an extended list of indices indicating what elements of the sequence will be where.
        ext_indices_tensor = torch.Tensor([circular_index(shift, num_addr) for shift in range(-shift_size//2+1,  num_addr+shift_size//2)]).long()
        logger.debug("ext_indices {}:\n {}".format(ext_indices_tensor.size(),  ext_indices_tensor))
    
        # Use indices for creation of an extended attention vector.
        ext_attention_BxEAx1 = torch.index_select(attention_BxAx1,  dim=1,  index=ext_indices_tensor)
        logger.debug("ext_attention_BxEAx1 {}:\n {}".format(ext_attention_BxEAx1.size(),  ext_attention_BxEAx1))    
        
        # Transpose inputs to convolution.
        ext_att_trans_Bx1xEA = torch.transpose(ext_attention_BxEAx1,  1, 2)
        shift_trans_Bx1xS = torch.transpose(shift_BxSx1,  1,  2)
        # Perform  convolution for every batch-filter pair.
        tmp_attention_list = []
        for b in range(batch_size):
            tmp_attention_list.append(F.conv1d(ext_att_trans_Bx1xEA.narrow(0, b, 1),  shift_trans_Bx1xS.narrow(0, b, 1)))
        # Concatenate list into a single tensor.
        shifted_attention_BxAx1 = torch.transpose(torch.cat(tmp_attention_list,  dim=0),  1,  2)
        logger.debug("shifted_attention_BxAx1 {}:\n {}".format(shifted_attention_BxAx1.size(),  shifted_attention_BxAx1))
        
        # Manual test of convolution
        #sum = 0
        #el =0
        #b = 0
        #for i in range(3):
        #    sum += ext_attention_BxEAx1[b][el+i][0] * shift_BxSx1[b][i][0]
        #print("SUM= ", sum)
        return shifted_attention_BxAx1

    def sharpening(self,  attention_BxAx1,  gamma_Bx1x1):
        """ Performs attention sharpening.
        
        :param attention_BxAx1: Current attention [BATCH_SIZE x ADDRESS_SIZE x 1]
        :param gamma_Bx1x1: sharpening factor [BATCH_SIZE x 1 x 1]
        :returns: attention vector of size [BATCH_SIZE x ADDRESS_SIZE x 1]
        """
        #gamma_Bx1x1[0][0][0]=40
        #gamma_Bx1x1[0][0][0]=10
        
        logger.debug("gamma_Bx1x1 {}:\n {}".format(gamma_Bx1x1.size(),  gamma_Bx1x1))
                    
        # Power.        
        pow_attention_BxAx1 = torch.pow(attention_BxAx1,  gamma_Bx1x1)
        logger.debug("pow_attention_BxAx1 {}:\n {}".format(pow_attention_BxAx1.size(),  pow_attention_BxAx1))
        
        # Normalize along addresses. 
        norm_attention_BxAx1 = F.normalize(pow_attention_BxAx1, p=1,  dim=1)
        logger.debug("norm_attention_BxAx1 {}:\n {}".format(norm_attention_BxAx1.size(),  norm_attention_BxAx1))
  
        return norm_attention_BxAx1
